// ---------------------------------------------------------------------------
// keccak_recovered -- the RTL reconstructed from keccak_toy.vo alone.
//
// Written from the netlist, in the netlist's own numbering.  Every constant
// below is traceable to one step of analysis.py, and none of it was copied
// from design.v or spec.md:
//
//   the 5 x 5 x 8 grid     step "parity": forty cells are a pure XOR of five
//                          flip-flops, partitioning the 200 into forty classes
//                          {i, i+40, i+80, i+120, i+160}; the unrotated parity
//                          neighbour of a class has order 5 and composes with
//                          the rotated one into an 8-cycle.  So: five columns,
//                          eight bits per lane, five rows -- and flip-flop i is
//                          lane ((i/8) % 5, i/40) at bit i % 8.
//   the theta feedback     step "theta": every theta cell is a 3-input XOR of
//                          one flip-flop and two parity nets, one at the same
//                          bit index and one column left, one at the next bit
//                          index and one column right.
//   the rho offsets        step "rhopi": the composite rho-then-pi is the
//                          200-bit map from each chi cell's b0 operand to the
//                          flip-flop its output reaches.  The bit-index shift
//                          is constant across each lane; those 25 constants are
//                          the table below.
//   the pi map             step "rhopi": lane (x,y) -> (y, 2x+3y), derived from
//                          the same permutation, not assumed.
//   chi                    step "chi": 200 cells of three theta nets each,
//                          whose ANF is b0 + b2 + b1*b2, i.e.
//                          b0 ^ (~b1 & b2); chi's own b1 chain closes into
//                          40 five-cycles, which are the rows.
//   the round constants    step "iota": eight cells over the five counter
//                          flip-flops, four of them constant zero, enumerated
//                          for counter values 0..17.
//   eighteen rounds        step "rounds": one five-input terminal-count cell,
//                          true for exactly one counter value, 17.
//   the enable             step "registers": 200 flip-flops share one `ena`
//                          net; the counter and the two flags are ungated.
//
// Re-synthesising this file under the settings in quartus/ gives the same
// primitive census as the original export.  That is corroboration, not an
// equivalence proof -- two designs can share a census and differ in wiring.
// The bounded netlist-versus-model check in section 7 is the stronger claim,
// and it is still bounded.
// ---------------------------------------------------------------------------

`default_nettype none

module keccak_recovered (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         start,
    input  wire [199:0] din,
    output wire [199:0] dout,
    output wire         done,
    output wire         busy
);

    localparam integer W       = 8;
    localparam integer NROUNDS = 18;

    // flip-flop i is lane (x, y) bit z with lane index x + 5y = i / 8.
    function integer lane;
        input integer x;
        input integer y;
        lane = x + 5 * y;
    endfunction

    // The 25 recovered rotation amounts, indexed 5*x + y.  Already reduced
    // mod 8, because a bit-index difference is all a netlist can carry.
    function integer rho;
        input integer x;
        input integer y;
        case (5 * x + y)
            0:  rho = 0;   1:  rho = 4;   2:  rho = 3;   3:  rho = 1;
            4:  rho = 2;   5:  rho = 1;   6:  rho = 4;   7:  rho = 2;
            8:  rho = 5;   9:  rho = 2;   10: rho = 6;   11: rho = 6;
            12: rho = 3;   13: rho = 7;   14: rho = 5;   15: rho = 4;
            16: rho = 7;   17: rho = 1;   18: rho = 5;   19: rho = 0;
            20: rho = 3;   21: rho = 4;   22: rho = 7;   23: rho = 0;
            24: rho = 6;
            default: rho = 0;
        endcase
    endfunction

    reg [199:0] s;
    reg [4:0]   rnd;
    reg         run;
    reg         fin;

    wire load /* synthesis keep */;
    assign load = start & ~run;

    wire last /* synthesis keep */;
    assign last = (rnd == NROUNDS - 1);

    // ---- the forty column-parity cells --------------------------------------
    wire [39:0]  cpar  /* synthesis keep */;
    wire [199:0] theta /* synthesis keep */;
    wire [199:0] b;
    wire [199:0] chi   /* synthesis keep */;

    genvar gx, gy, gz;
    generate
        for (gx = 0; gx < 5; gx = gx + 1) begin : column
            for (gz = 0; gz < W; gz = gz + 1) begin : parity
                assign cpar[W * gx + gz] =
                      s[W * lane(gx, 0) + gz] ^ s[W * lane(gx, 1) + gz]
                    ^ s[W * lane(gx, 2) + gz] ^ s[W * lane(gx, 3) + gz]
                    ^ s[W * lane(gx, 4) + gz];
            end
        end

        for (gx = 0; gx < 5; gx = gx + 1) begin : tx
            for (gy = 0; gy < 5; gy = gy + 1) begin : ty
                for (gz = 0; gz < W; gz = gz + 1) begin : tz
                    assign theta[W * lane(gx, gy) + gz] =
                          s[W * lane(gx, gy) + gz]
                        ^ cpar[W * ((gx + 4) % 5) + gz]
                        ^ cpar[W * ((gx + 1) % 5) + ((gz + W - 1) % W)];
                end
            end
        end

        // rho and pi: zero cells in the export, so zero cells here.
        for (gx = 0; gx < 5; gx = gx + 1) begin : bx
            for (gy = 0; gy < 5; gy = gy + 1) begin : by
                for (gz = 0; gz < W; gz = gz + 1) begin : bz
                    assign b[W * lane(gy, (2 * gx + 3 * gy) % 5) + gz] =
                        theta[W * lane(gx, gy) + ((gz + W - rho(gx, gy)) % W)];
                end
            end
        end

        for (gx = 0; gx < 5; gx = gx + 1) begin : cx
            for (gy = 0; gy < 5; gy = gy + 1) begin : cy
                for (gz = 0; gz < W; gz = gz + 1) begin : cz
                    assign chi[W * lane(gx, gy) + gz] =
                          b[W * lane(gx, gy) + gz]
                        ^ (~b[W * lane((gx + 1) % 5, gy) + gz]
                           & b[W * lane((gx + 2) % 5, gy) + gz]);
                end
            end
        end
    endgenerate

    // ---- the eighteen recovered round constants ------------------------------
    reg [7:0] rc_lut /* synthesis keep */;

    always @* begin
        case (rnd)
            5'd0:  rc_lut = 8'h01;   5'd1:  rc_lut = 8'h82;
            5'd2:  rc_lut = 8'h8a;   5'd3:  rc_lut = 8'h00;
            5'd4:  rc_lut = 8'h8b;   5'd5:  rc_lut = 8'h01;
            5'd6:  rc_lut = 8'h81;   5'd7:  rc_lut = 8'h09;
            5'd8:  rc_lut = 8'h8a;   5'd9:  rc_lut = 8'h88;
            5'd10: rc_lut = 8'h09;   5'd11: rc_lut = 8'h0a;
            5'd12: rc_lut = 8'h8b;   5'd13: rc_lut = 8'h8b;
            5'd14: rc_lut = 8'h89;   5'd15: rc_lut = 8'h03;
            5'd16: rc_lut = 8'h02;   5'd17: rc_lut = 8'h80;
            default: rc_lut = 8'h00;
        endcase
    end

    wire [199:0] rnd_out;

    generate
        for (gz = 0; gz < 200; gz = gz + 1) begin : iota
            if (gz < W) begin : lane00
                assign rnd_out[gz] = chi[gz] ^ rc_lut[gz];
            end else begin : rest
                assign rnd_out[gz] = chi[gz];
            end
        end
    endgenerate

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s   <= 200'd0;
            rnd <= 5'd0;
            run <= 1'b0;
            fin <= 1'b0;
        end else if (load) begin
            s   <= din;
            rnd <= 5'd0;
            run <= 1'b1;
            fin <= 1'b0;
        end else if (run) begin
            s   <= rnd_out;
            rnd <= rnd + 5'd1;
            if (last) begin
                run <= 1'b0;
                fin <= 1'b1;
            end
        end
    end

    assign dout = s;
    assign done = fin;
    assign busy = run;

endmodule

`default_nettype wire
