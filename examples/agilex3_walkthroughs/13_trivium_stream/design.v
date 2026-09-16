// ---------------------------------------------------------------------------
// trivium_stream -- the "original design" of Agilex 3 walkthrough 13.
//
// Trivium (De Canniere and Preneel, ISC 2006; eSTREAM portfolio, ISO/IEC
// 29192-3), keystream generation only: an 80-bit key, an 80-bit IV, 288 bits of
// state held as three coupled shift registers of 93, 84 and 111 stages, a
// 1152-cycle warm-up, and one keystream bit per clock cycle after it.
//
// The whole cipher is one line of arithmetic per segment:
//
//     t1 = s66 ^ s93  ^ (s91  & s92 ) ^ s171     -> head of segment B
//     t2 = s162 ^ s177 ^ (s175 & s176) ^ s264    -> head of segment C
//     t3 = s243 ^ s288 ^ (s286 & s287) ^ s69     -> head of segment A
//     z  = s66 ^ s93 ^ s162 ^ s177 ^ s243 ^ s288 -> the keystream bit
//
// and everything else is a shift.  The three `&` are the entire difference
// between this and the LFSR of walkthrough 05: without them the next-state map
// is linear, the register has a feedback polynomial, and the output is a
// maximal-length sequence rather than a cipher.  Finding those three AND gates
// in the exported netlist is what the walkthrough is about.
//
// Deliberately *not* used, so that the post-synthesis netlist contains only
// primitives that plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models
// (tennm_lcell_comb, tennm_ff and the two constant cells the importer adds):
// no memory block (see AUTO_SHIFT_REGISTER_RECOGNITION in the QSF -- three long
// shift registers are exactly what Quartus wants to retarget onto RAM), no DSP,
// no PLL, no I/O buffers (the flow stops after synthesis, before fitting), no
// second clock domain, and -- via ALLOW_SYNCH_CTRL_USAGE OFF -- no use of the
// flip-flop's sload/sclr pins.
// ---------------------------------------------------------------------------

`default_nettype none

module trivium_stream (
    // The only clock in the design.
    input  wire        clk,

    // Active-low asynchronous reset.  Quartus maps this onto every ALM
    // register's dedicated `clrn` pin, so in the netlist it is a net that
    // reaches every flip-flop's clrn and nothing else.
    input  wire        rst_n,

    // Load key/iv and begin the warm-up.  Ignored while `busy` is high, which
    // is what makes the 1152-cycle warm-up uninterruptible.
    input  wire        start,

    // key[i] is the specification's K(i+1); iv[i] is IV(i+1).
    input  wire [79:0] key,
    input  wire [79:0] iv,

    // The keystream bit, driven continuously.  It only *means* keystream while
    // `ks_valid` is high -- but driving it always means a wrong feedback tap
    // reaches an output in tens of cycles instead of after the warm-up.
    output wire        ks,
    output wire        ks_valid,
    output wire        busy
);

    // ---- cipher parameters -------------------------------------------------
    // Segment boundaries in the specification's 1-based numbering:
    //   A = s1..s93, B = s94..s177, C = s178..s288.
    // In this RTL the state vector is 0-based, so spec bit sN is s[N-1].
    localparam integer WARMUP_LO = 64;   // 64 * 18 = 1152 = 4 * 288
    localparam integer WARMUP_HI = 18;

    // ---- state -------------------------------------------------------------
    reg [287:0] s;        // the whole cipher state, three segments end to end
    reg [5:0]   lo;       // warm-up counter, low half: 0..63
    reg [4:0]   hi;       // warm-up counter, high half: 0..17
    reg         warm;     // the warm-up is running
    reg         done;     // the warm-up finished; ks is keystream

    // A start is accepted only when idle, exactly as in walkthrough 11.
    wire load = start & ~warm;

    // ---- the load pattern ---------------------------------------------------
    // (s1..s93)   = (K1..K80, 0 x13)
    // (s94..s177) = (IV1..IV80, 0 x4)
    // (s178..s288)= (0 x108, 1, 1, 1)
    wire [287:0] init = {3'b111, 108'd0, 4'd0, iv, 13'd0, key};

    // ---- the three feedback functions --------------------------------------
    // `keep` is a coverage requirement, not a style choice: without it Quartus
    // flattens `load ? K1 : t3` into one seven-input cone, which needs the ALM's
    // fracturable seven/eight-input mode (extended_lut "on") -- outside the
    // primitive configuration tools/hal_agilex validated.  Kept, it is a
    // five-input feedback cell feeding a three-input multiplexer, both inside
    // coverage.  spec.md says so too.
    wire t1 /* synthesis keep */;
    wire t2 /* synthesis keep */;
    wire t3 /* synthesis keep */;

    assign t1 = s[65]  ^ s[92]  ^ (s[90]  & s[91])  ^ s[170];
    assign t2 = s[161] ^ s[176] ^ (s[174] & s[175]) ^ s[263];
    assign t3 = s[242] ^ s[287] ^ (s[285] & s[286]) ^ s[68];

    // ---- the keystream bit --------------------------------------------------
    // Six state bits, one ALM: the *linear* part of the three feedbacks, with
    // the three AND terms left out.  That is the cipher's output function.
    assign ks = s[65] ^ s[92] ^ s[161] ^ s[176] ^ s[242] ^ s[287];

    // ---- the warm-up counter, as two counters -------------------------------
    // 1152 needs 11 bits and `count == 1151` is an 11-input test; six is what an
    // ALM inside the validated coverage reads.  64 * 18 splits it into a 6-input
    // test and a 5-input one.  Both are kept so they stay separate cells.
    wire lo_last /* synthesis keep */;
    wire hi_last /* synthesis keep */;
    assign lo_last = (lo == WARMUP_LO - 1);
    assign hi_last = (hi == WARMUP_HI - 1);
    wire last = warm & lo_last & hi_last;

    // ---- the register ------------------------------------------------------
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s    <= 288'd0;
            lo   <= 6'd0;
            hi   <= 5'd0;
            warm <= 1'b0;
            done <= 1'b0;
        end else begin
            // --- the state: load, or one Trivium step ----------------------
            // There is no "hold": shifting the all-zero state gives the
            // all-zero state, so an idle core needs no third multiplexer input
            // and every stage's next state is two signals wide.
            if (load) begin
                s <= init;
            end else begin
                s[0] <= t3;
                for (i = 1; i < 93; i = i + 1) s[i] <= s[i-1];
                s[93] <= t1;
                for (i = 94; i < 177; i = i + 1) s[i] <= s[i-1];
                s[177] <= t2;
                for (i = 178; i < 288; i = i + 1) s[i] <= s[i-1];
            end

            // --- the warm-up counter: runs only while `warm` ----------------
            if (load) begin
                lo <= 6'd0;
                hi <= 5'd0;
            end else if (warm) begin
                lo <= lo + 6'd1;
                if (lo_last) hi <= hi + 5'd1;
            end

            // --- the two flags ----------------------------------------------
            if (load) begin
                warm <= 1'b1;
                done <= 1'b0;
            end else if (last) begin
                warm <= 1'b0;
                done <= 1'b1;
            end
        end
    end

    assign busy     = warm;
    assign ks_valid = done;

endmodule

`default_nettype wire
