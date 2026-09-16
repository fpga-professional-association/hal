// ---------------------------------------------------------------------------
// keccak_retimed -- the counterfactual export of Agilex 3 walkthrough 14.
//
// The *same* permutation as design.v, with the *same* interface, the same
// nineteen cycles from an accepted `start` to `done`, and the same result on
// `dout` when `done` is high.  One thing is different: where the register sits
// inside the round.
//
//   design.v   the register holds s_k, the state entering round k, and one
//              cycle computes  iota . chi . pi . rho . theta (s_k).
//              chi therefore reads the register bank through theta, and
//              *every* chi cone depends on thirty-three flip-flops.
//
//   this file  the register holds  pi.rho.theta(s_k)  instead -- the round is
//              rotated by half a step, which is a standard retiming and costs
//              nothing but a multiplexer on the last round.  chi is now the
//              *first* combinational layer after the flip-flops, and each chi
//              cone depends on exactly three of them.
//
// Nothing about the algorithm changed.  What changed is whether
// `hal_crypto sbox` can see it: forty 5-bit substitutions sitting on register
// outputs are exactly the shape the pass looks for, and thirty-three-input
// cones are exactly the shape it refuses to enumerate.  Section 10 of the
// guide runs both exports through the same command and prints both answers.
//
// The algebra, so the rotation is checkable rather than asserted.  Write
// g = pi.rho.theta and f_k = iota_k.chi, so one round is R_k = f_k . g and
// Keccak-f[200] is R_17 . ... . R_0.  With V_k = g(s_k):
//
//     V_0     = g(din)                       <- the load cycle
//     V_{k+1} = g(f_k(V_k))      k = 0..16   <- the ordinary round cycles
//     s_18    = f_17(V_17)                   <- the last round, stored raw
//
// which is why the next-state multiplexer reads `last`: on the final round the
// register takes f_17(V_17) instead of g(f_17(V_17)).  On the load cycle it
// takes g(din), and since the multiplexer that chooses between `din` and the
// round output sits *before* theta, that falls out of the same wires.
// ---------------------------------------------------------------------------

`default_nettype none

module keccak_retimed (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         start,
    input  wire [199:0] din,

    // The register, driven continuously.  Unlike design.v it does *not* carry
    // the round state while `busy` is high -- it carries pi.rho.theta of it --
    // so only the `done` cycle is comparable between the two exports.  That is
    // itself a fact a reverse engineer can measure, and section 10 does.
    output wire [199:0] dout,
    output wire         done,
    output wire         busy
);

    localparam integer W       = 8;
    localparam integer NROUNDS = 18;

    function integer lane;
        input integer x;
        input integer y;
        lane = x + 5 * y;
    endfunction

    // The rho offsets mod 8, exactly as in design.v.
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

    reg [199:0] v;      // pi.rho.theta of the round state
    reg [4:0]   rnd;
    reg         run;
    reg         fin;

    wire load /* synthesis keep */;
    assign load = start & ~run;

    wire last /* synthesis keep */;
    assign last = (rnd == NROUNDS - 1);

    // ---- chi, now the first layer after the flip-flops ----------------------
    // Three register outputs into one ALM, two hundred of them.  This is the
    // whole point of the file: `keep` holds them apart from the load
    // multiplexer, so each cone's support is three flip-flops and nothing else.
    wire [199:0] chi /* synthesis keep */;

    genvar gx, gy, gz;
    generate
        for (gx = 0; gx < 5; gx = gx + 1) begin : cx
            for (gy = 0; gy < 5; gy = gy + 1) begin : cy
                for (gz = 0; gz < W; gz = gz + 1) begin : cz
                    assign chi[W * lane(gx, gy) + gz] =
                          v[W * lane(gx, gy) + gz]
                        ^ (~v[W * lane((gx + 1) % 5, gy) + gz]
                           & v[W * lane((gx + 2) % 5, gy) + gz]);
                end
            end
        end
    endgenerate

    // ---- iota ---------------------------------------------------------------
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

    // a = iota(chi(v)) = the round state s_{k+1}, and on the load cycle `din`.
    wire [199:0] a;
    wire [199:0] pre /* synthesis keep */;

    generate
        for (gz = 0; gz < 200; gz = gz + 1) begin : iota
            if (gz < W) begin : lane00
                assign a[gz] = chi[gz] ^ rc_lut[gz];
            end else begin : rest
                assign a[gz] = chi[gz];
            end
        end
    endgenerate

    assign pre = load ? din : a;

    // ---- theta, rho, pi over `pre` ------------------------------------------
    wire [39:0]  cpar  /* synthesis keep */;
    wire [199:0] theta /* synthesis keep */;
    wire [199:0] g;

    generate
        for (gx = 0; gx < 5; gx = gx + 1) begin : column
            for (gz = 0; gz < W; gz = gz + 1) begin : parity
                assign cpar[W * gx + gz] =
                      pre[W * lane(gx, 0) + gz] ^ pre[W * lane(gx, 1) + gz]
                    ^ pre[W * lane(gx, 2) + gz] ^ pre[W * lane(gx, 3) + gz]
                    ^ pre[W * lane(gx, 4) + gz];
            end
        end

        for (gx = 0; gx < 5; gx = gx + 1) begin : tx
            for (gy = 0; gy < 5; gy = gy + 1) begin : ty
                for (gz = 0; gz < W; gz = gz + 1) begin : tz
                    assign theta[W * lane(gx, gy) + gz] =
                          pre[W * lane(gx, gy) + gz]
                        ^ cpar[W * ((gx + 4) % 5) + gz]
                        ^ cpar[W * ((gx + 1) % 5) + ((gz + W - 1) % W)];
                end
            end
        end

        // rho and pi: pure wiring here too.
        for (gx = 0; gx < 5; gx = gx + 1) begin : bx
            for (gy = 0; gy < 5; gy = gy + 1) begin : by
                for (gz = 0; gz < W; gz = gz + 1) begin : bz
                    assign g[W * lane(gy, (2 * gx + 3 * gy) % 5) + gz] =
                        theta[W * lane(gx, gy) + ((gz + W - rho(gx, gy)) % W)];
                end
            end
        end
    endgenerate

    // ---- the register -------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            v   <= 200'd0;
            rnd <= 5'd0;
            run <= 1'b0;
            fin <= 1'b0;
        end else if (load) begin
            v   <= g;          // = pi.rho.theta(din), since pre = din
            rnd <= 5'd0;
            run <= 1'b1;
            fin <= 1'b0;
        end else if (run) begin
            v   <= last ? pre : g;
            rnd <= rnd + 5'd1;
            if (last) begin
                run <= 1'b0;
                fin <= 1'b1;
            end
        end
    end

    assign dout = v;
    assign done = fin;
    assign busy = run;

endmodule

`default_nettype wire
