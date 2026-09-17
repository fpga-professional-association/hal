// ---------------------------------------------------------------------------
// core_a -- GROUND TRUTH for walkthrough 16.  Do not read this while doing the
// exercise; cores/core_a.anon.hal.v is the exercise.
//
// This is walkthrough 14's design.v verbatim below the header, with the
// top-level module renamed `core_a` so that it synthesizes into its own Quartus
// revision.  Nothing else was touched: the algorithm, the parameters, the
// coding style and the synthesis-shaping comments are the originals, which is
// what makes the answer key exact.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// core_a -- the "original design" of Agilex 3 walkthrough 14.
//
// Keccak-f[200]: the smallest member of the Keccak-f permutation family
// (Bertoni, Daemen, Peeters and Van Assche, "The Keccak reference", version
// 3.0, 2011; the same construction is standardised at width 1600 as
// KECCAK-p[1600,24] in NIST FIPS 202).  Two hundred bits of state held as a
// 5 x 5 array of 8-bit lanes, eighteen rounds, **one round per clock cycle**.
//
// One round is five steps, in this order:
//
//     theta   A[x][y] ^= C[x-1] ^ ROTL(C[x+1],1),  C[x] = XOR of column x
//     rho     each lane rotated by its own fixed offset
//     pi      each lane moved to (y, 2x+3y)
//     chi     A[x][y] = B[x][y] ^ (~B[x+1][y] & B[x+2][y])     <- the only
//                                                                 nonlinearity
//     iota    A[0][0] ^= RC[round]
//
// theta and chi cost logic.  rho and pi cost **nothing**: rotating a lane and
// relabelling which lane it is are both pure wiring, so after synthesis there
// is no net, no cell and no vector that says "rotate by five" -- exactly the
// shape walkthrough 11's ARX rotations had.  Recovering them means reading
// index arithmetic off an ordered cell layer, and that is half of what this
// walkthrough is about.  The other half is chi: a 5-bit substitution on every
// row of the state, and the one step of the round that is not linear over
// GF(2).
//
// Deliberately *not* used, so that the post-synthesis netlist contains only
// primitives that plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models
// (tennm_lcell_comb, tennm_ff and the two constant cells the importer adds):
// no memory block, no DSP, no PLL, no I/O buffers (the flow stops after
// synthesis, before fitting), no second clock domain, and -- via
// ALLOW_SYNCH_CTRL_USAGE OFF -- no use of the flip-flop's sload/sclr pins.
//
// The `keep` attributes on cpar/theta/chi/rc_lut are a *coverage* requirement
// and not a style choice; quartus/core_a.qsf and spec.md both say why.
// Without them Quartus is free to flatten a chi cell and its load multiplexer
// into one seven-input cone, which needs the ALM's fracturable seven/eight
// input mode (extended_lut "on") -- outside the primitive configuration
// tools/hal_agilex validated.  Walkthrough 13 hit the same wall.
// ---------------------------------------------------------------------------

`default_nettype none

module core_a (
    // The only clock in the design.
    input  wire         clk,

    // Active-low asynchronous reset.  Quartus maps this onto every ALM
    // register's dedicated `clrn` pin.
    input  wire         rst_n,

    // Load `din` and run the permutation.  Ignored while `busy` is high, so a
    // permutation once begun always runs its eighteen rounds to completion.
    input  wire         start,

    // The state, as the sponge literature orders it: lane (x,y) is byte
    // x + 5*y, and bit z of that lane is bit 8*(x + 5*y) + z.  So din is the
    // 25-byte state string, little-endian within each lane.
    input  wire [199:0] din,

    // The state register, driven continuously.  It only *means*
    // "Keccak-f[200](din)" while `done` is high -- but driving it always means
    // a wrong rotation offset reaches an output one cycle after a load instead
    // of only after eighteen rounds.  Same testability decision as
    // walkthrough 13's `ks`.
    output wire [199:0] dout,
    output wire         done,
    output wire         busy
);

    // ---- permutation parameters --------------------------------------------
    localparam integer W       = 8;    // lane width; l = 3
    localparam integer NROUNDS = 18;   // 12 + 2*l

    // lane (x,y) occupies state bits [8*lane(x,y) +: 8]
    function integer lane;
        input integer x;
        input integer y;
        lane = x + 5 * y;
    endfunction

    // The rho offsets, reduced mod 8.  The published table is stated at width
    // 64; at w = 8 only the residue matters.  Index is 5*x + y.
    function integer rho;
        input integer x;
        input integer y;
        case (5 * x + y)
            //  x  y   published   mod 8
            0:  rho = 0;   //  0  0      0        0
            1:  rho = 4;   //  0  1     36        4
            2:  rho = 3;   //  0  2      3        3
            3:  rho = 1;   //  0  3     41        1
            4:  rho = 2;   //  0  4     18        2
            5:  rho = 1;   //  1  0      1        1
            6:  rho = 4;   //  1  1     44        4
            7:  rho = 2;   //  1  2     10        2
            8:  rho = 5;   //  1  3     45        5
            9:  rho = 2;   //  1  4      2        2
            10: rho = 6;   //  2  0     62        6
            11: rho = 6;   //  2  1      6        6
            12: rho = 3;   //  2  2     43        3
            13: rho = 7;   //  2  3     15        7
            14: rho = 5;   //  2  4     61        5
            15: rho = 4;   //  3  0     28        4
            16: rho = 7;   //  3  1     55        7
            17: rho = 1;   //  3  2     25        1
            18: rho = 5;   //  3  3     21        5
            19: rho = 0;   //  3  4     56        0
            20: rho = 3;   //  4  0     27        3
            21: rho = 4;   //  4  1     20        4
            22: rho = 7;   //  4  2     39        7
            23: rho = 0;   //  4  3      8        0
            24: rho = 6;   //  4  4     14        6
            default: rho = 0;
        endcase
    endfunction

    // ---- state -------------------------------------------------------------
    reg [199:0] s;      // the permutation state, 25 lanes of 8 bits
    reg [4:0]   rnd;    // the round counter, 0 .. 17
    reg         run;    // a permutation is in progress
    reg         fin;    // the permutation finished; `s` is the result

    // A start is accepted only when idle, exactly as in walkthroughs 11 and 13.
    // `keep` again, and again for coverage rather than style: unkept, Quartus
    // inlines `start & ~run` into the round counter's top bit, whose next state
    // is then a seven-input cone (start, run and all five counter bits) and
    // needs the fracturable ALM mode.  Kept, `load` is one cell that 208 others
    // read -- every register's next-state logic plus the shared clock enable --
    // and the counter's top bit is back to six inputs.
    wire load /* synthesis keep */;
    assign load = start & ~run;

    // ---- theta: five parity planes -----------------------------------------
    // cpar[8*x + z] = C[x][z], the XOR of the five lanes in column x.  Five
    // state bits into one ALM, forty of them.  `keep` holds them as their own
    // cells: merged into theta they would make a nine-input cone.
    wire [39:0] cpar /* synthesis keep */;

    // theta[8*lane(x,y) + z] = A[x][y][z] ^ C[x-1][z] ^ C[x+1][z-1].
    // Three inputs: one state bit and two parity bits.
    wire [199:0] theta /* synthesis keep */;

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
                    // D[x] = C[x-1] ^ ROTL(C[x+1], 1), so bit z of D[x] reads
                    // bit z of column x-1 and bit z-1 of column x+1.
                    assign theta[W * lane(gx, gy) + gz] =
                          s[W * lane(gx, gy) + gz]
                        ^ cpar[W * ((gx + 4) % 5) + gz]
                        ^ cpar[W * ((gx + 1) % 5) + ((gz + W - 1) % W)];
                end
            end
        end
    endgenerate

    // ---- rho and pi: no logic at all ---------------------------------------
    // rho rotates lane (x,y) left by rho(x,y); pi moves it to (y, 2x+3y).
    // Both are pure renaming, so `b` costs not one cell: after synthesis the
    // chi cells simply read differently-indexed `theta` nets, and the offsets
    // exist only as that indexing.
    wire [199:0] b;

    generate
        for (gx = 0; gx < 5; gx = gx + 1) begin : bx
            for (gy = 0; gy < 5; gy = gy + 1) begin : by
                for (gz = 0; gz < W; gz = gz + 1) begin : bz
                    // bit z of ROTL(v, r) is bit z-r of v
                    assign b[W * lane(gy, (2 * gx + 3 * gy) % 5) + gz] =
                        theta[W * lane(gx, gy) + ((gz + W - rho(gx, gy)) % W)];
                end
            end
        end
    endgenerate

    // ---- chi: the 5-bit row map, and the only nonlinear step ----------------
    // chi[x][y] = b[x][y] ^ (~b[x+1][y] & b[x+2][y]).  Three inputs, one ALM,
    // two hundred of them.  Every row of five lanes is one 5-bit substitution
    // applied bit-slice by bit-slice; the AND is what makes it a permutation
    // of degree two rather than a linear layer.
    wire [199:0] chi /* synthesis keep */;

    generate
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

    // ---- iota: one round constant into lane (0,0) --------------------------
    // RC[i] is nonzero only at bit positions 2^j - 1 for j = 0..3, i.e. bits
    // 0, 1, 3 and 7 of the lane, so only four of the two hundred next-state
    // cells read the round counter at all.  The other four bits of lane (0,0)
    // are constant zero across all eighteen rounds and disappear.
    reg [7:0] rc_lut /* synthesis keep */;

    always @* begin
        case (rnd)
            5'd0:  rc_lut = 8'h01;
            5'd1:  rc_lut = 8'h82;
            5'd2:  rc_lut = 8'h8a;
            5'd3:  rc_lut = 8'h00;
            5'd4:  rc_lut = 8'h8b;
            5'd5:  rc_lut = 8'h01;
            5'd6:  rc_lut = 8'h81;
            5'd7:  rc_lut = 8'h09;
            5'd8:  rc_lut = 8'h8a;
            5'd9:  rc_lut = 8'h88;
            5'd10: rc_lut = 8'h09;
            5'd11: rc_lut = 8'h0a;
            5'd12: rc_lut = 8'h8b;
            5'd13: rc_lut = 8'h8b;
            5'd14: rc_lut = 8'h89;
            5'd15: rc_lut = 8'h03;
            5'd16: rc_lut = 8'h02;
            5'd17: rc_lut = 8'h80;
            default: rc_lut = 8'h00;
        endcase
    end

    // The round output: chi everywhere, chi ^ RC on the eight bits of lane
    // (0,0).
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

    // ---- the round counter --------------------------------------------------
    // Eighteen rounds needs five bits and `rnd == 17` is a five-input test, so
    // unlike walkthrough 13's 1152 it fits one ALM and needs no splitting.
    wire last /* synthesis keep */;
    assign last = (rnd == NROUNDS - 1);

    // ---- the register -------------------------------------------------------
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
            // One round per cycle.  There is no third multiplexer input for
            // "hold": idle is the flip-flop's clock enable being low, which is
            // one net for all 200 stages instead of one more data pin each.
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
