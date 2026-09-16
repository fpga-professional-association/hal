// ---------------------------------------------------------------------------
// trivium_stream, reconstructed from trivium_stream.vo alone.
//
// Every constant below is traceable to one step of analysis.py, and to nothing
// else -- design.v and spec.md were not read while writing this file:
//
//   * the register names s[287:0], lo[5:0], hi[4:0], warm, done, and the ports,
//     are the export's own (step `stats` / `registers`);
//   * 93 / 84 / 111 and which stage follows which come from the chain walk with
//     `start` held at 0 (step `chains`);
//   * the three feedback equations and the one AND term in each come from the
//     algebraic normal form of the three head cones (step `feedback`);
//   * the six bits of the output function come from the cone of the cell that
//     drives `ks` (step `output`);
//   * the load pattern -- key at s[79:0], iv at s[172:93], ones at s[287:285],
//     zeros everywhere else -- comes from cofactoring every stage's next state
//     at the assignment that stops it reading its predecessor (step `load`);
//   * 64 and 18, and therefore 1152 = 4 * 288, come from the two terminal-count
//     cells (step `warmup`);
//   * that 1153 cycles pass from an accepted start to `ks_valid`, and that the
//     result is the published eSTREAM keystream, comes from driving the export
//     (step `keystream`).
//
// What is *not* recovered, and is a free choice here: the spelling of the
// control logic. The export gives `hi` a clock enable rather than a
// multiplexer, which is a Quartus packing decision; written as an `if` below,
// the same synthesis reproduces it.
// ---------------------------------------------------------------------------

`default_nettype none

module trivium_stream (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    input  wire [79:0] key,
    input  wire [79:0] iv,
    output wire        ks,
    output wire        ks_valid,
    output wire        busy
);

    reg [287:0] s;
    reg [5:0]   lo;
    reg [4:0]   hi;
    reg         warm;
    reg         done;

    // step `load`: the multiplexer select is (start = 1, warm = 0).
    wire load = start & ~warm;

    // step `feedback`: three five-input cones, one product term each.  In the
    // cipher's 1-based numbering these read
    //   s66 + s93 + s91*s92 + s171   -> head of the 84-stage segment
    //   s162 + s177 + s175*s176 + s264 -> head of the 111-stage segment
    //   s243 + s288 + s286*s287 + s69  -> head of the 93-stage segment
    wire f_84  /* synthesis keep */;
    wire f_111 /* synthesis keep */;
    wire f_93  /* synthesis keep */;

    assign f_84  = s[65]  ^ s[92]  ^ (s[90]  & s[91])  ^ s[170];
    assign f_111 = s[161] ^ s[176] ^ (s[174] & s[175]) ^ s[263];
    assign f_93  = s[242] ^ s[287] ^ (s[285] & s[286]) ^ s[68];

    // step `output`: a pure six-input XOR, and every one of the six is a linear
    // tap of one of the three feedbacks.  None of the AND inputs is read here.
    assign ks = s[65] ^ s[92] ^ s[161] ^ s[176] ^ s[242] ^ s[287];

    // step `warmup`: one cell fires on lo == 63, one on hi == 17.
    wire lo_last /* synthesis keep */;
    wire hi_last /* synthesis keep */;
    assign lo_last = (lo == 6'd63);
    assign hi_last = (hi == 5'd17);
    wire last = warm & lo_last & hi_last;

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s    <= 288'd0;
            lo   <= 6'd0;
            hi   <= 5'd0;
            warm <= 1'b0;
            done <= 1'b0;
        end else begin
            if (load) begin
                // step `load`: 80 key bits, 13 zeros, 80 iv bits, 112 zeros,
                // three ones.
                s <= {3'b111, 112'd0, iv, 13'd0, key};
            end else begin
                s[0] <= f_93;
                for (i = 1; i < 93; i = i + 1) s[i] <= s[i-1];
                s[93] <= f_84;
                for (i = 94; i < 177; i = i + 1) s[i] <= s[i-1];
                s[177] <= f_111;
                for (i = 178; i < 288; i = i + 1) s[i] <= s[i-1];
            end

            if (load) begin
                lo <= 6'd0;
                hi <= 5'd0;
            end else if (warm) begin
                lo <= lo + 6'd1;
                if (lo_last) hi <= hi + 5'd1;
            end

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
