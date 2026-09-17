// ---------------------------------------------------------------------------
// ntt_mult -- the "original design" of Agilex 3 walkthrough 15.
//
// A toy negacyclic polynomial multiplier over R = Z_q[x] / (x^n + 1) with
// n = 16 and q = 257, built the way a lattice-scheme accelerator is built: a
// number-theoretic transform, a pointwise product, an inverse transform.
// One butterfly per clock cycle, one shared modular multiplier, 144 cycles per
// product.
//
//   c(x) = a(x) * b(x)  mod (x^16 + 1, 257)
//
// q = 257 = 2^8 + 1 is a Fermat prime, and that is the whole reason the design
// fits in ALM logic: reducing a 17-bit product modulo 2^8 + 1 is
//
//     P = 256*Ph + Pl   ==>   P = Pl - Ph   (mod 257)
//
// because 256 = -1 (mod 257) -- one subtraction and one conditional addition
// of 257, no division and no Montgomery/Barrett constant.  ML-KEM's q = 3329
// and ML-DSA's q = 8380417 have no such structure; this is a toy.
//
// The structure, not the parameters, is the point.  psi = 15 has order 32
// modulo 257, so psi^16 = -1 and omega = psi^2 = 225 is a primitive 16th root
// of unity.  The forward transform is a Cooley-Tukey (decimation-in-time)
// stack with the psi^i pre-twist *merged into the twiddles* -- the same trick
// Kyber's reference NTT uses -- so it takes the coefficient vector in natural
// order to the evaluation vector in bit-reversed order at no extra cost.
//
// Six phases, driven by one counter:
//
//   NTT    64 cycles   32 butterflies over pa, then 32 over pb
//   POINT  16 cycles   pb[i] <= pa[i] * pb[i]
//   BREV   16 cycles   pa[brv(i)] <= pb[i]        (bit-reversal: pure wiring)
//   INTT   32 cycles   32 butterflies over pa, with omega^-1 twiddles
//   POST   16 cycles   pb[brv(j)] <= pa[j] * (n^-1 * psi^-brv(j))
//
// The inverse transform is the *same* Cooley-Tukey butterfly, not a
// Gentleman-Sande one: a GS unit puts its multiplier after the adder, and
// building both shapes out of one datapath means a multiplexer loop that
// Quartus would reject.  The price is the BREV pass, which costs sixteen
// cycles and not one cell -- exactly the sort of trade a real core makes, and
// exactly the sort of layer that leaves no trace in the netlist.
//
// Deliberately *not* used, so that the post-synthesis netlist contains only
// primitives that plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models
// (tennm_lcell_comb, tennm_ff and the two constant cells the importer adds):
// no memory block, no PLL, no I/O buffers (the flow stops after synthesis,
// before fitting), no second clock domain, and -- via ALLOW_SYNCH_CTRL_USAGE
// OFF -- no use of the flip-flop's sload/sclr pins.
//
// And, the one new hazard this design brings: **no DSP block**.  `w * v` is a
// 9 x 9 multiply, which Quartus will happily put in a tennm_mac -- a primitive
// hal_agilex does not model, and one that would swallow the whole modular
// multiplier into a black box.  `multstyle = "logic"` below (and the matching
// DSP_BLOCK_BALANCING assignment in quartus/ntt_mult.qsf) forces it into ALMs.
// quartus/ntt_dsp.qsf is the counterfactual that drops the attribute; see
// spec.md section "What synthesis is expected to do" and the guide's last
// section for what that export looks like.
//
// The `keep` attributes are a coverage requirement and not a style choice.
// Without them Quartus merges the read multiplexers with the array-select
// multiplexer on top of them (7 inputs -> the fracturable ALM mode, outside
// the validated primitive configuration), and -- worse for the analysis --
// duplicates the butterfly's operand nets so that the adder and the subtracter
// no longer read the *same* two vectors, which is the one structural fact that
// makes a butterfly a butterfly.  Walkthroughs 11, 13 and 14 hit the same wall
// for their own reasons.
// ---------------------------------------------------------------------------

`default_nettype none

// The counterfactual export of section 10 is this same file synthesised with
// `ALLOW_DSP` defined and the two DSP assignments dropped from the .qsf --
// nothing else changes, which is the point.  See quartus/ntt_dsp.qsf.
`ifndef ALLOW_DSP
(* multstyle = "logic" *)
`endif
module ntt_mult (
    // The only clock in the design.
    input  wire         clk,

    // Active-low asynchronous reset.  Quartus maps this onto every ALM
    // register's dedicated `clrn` pin.
    input  wire         rst_n,

    // Load (a, b) and run the multiplication.  Ignored while `busy` is high,
    // so a product once begun always runs its 144 cycles to completion.
    input  wire         start,

    // The two input polynomials, 16 coefficients of 8 bits each, coefficient
    // i in bits [8*i +: 8].  Eight bits, not nine: every residue class modulo
    // 257 that a byte can name is then already canonical, so nothing in the
    // datapath ever has to reduce an input.  (257 residues do not fit in a
    // byte; the missing one is 256 = -1, which the datapath produces
    // internally and can return, but cannot be handed.)
    input  wire [127:0] a_in,
    input  wire [127:0] b_in,

    // The product, 16 coefficients of 9 bits each, coefficient i in bits
    // [9*i +: 9], each in [0, 256].  Driven continuously from the pb register
    // bank: it only *means* a*b while `done` is high, but driving it always
    // means a wrong twiddle constant reaches an output inside the transform
    // that used it instead of only at the end.  Same testability decision as
    // walkthroughs 13 and 14.
    output wire [143:0] c_out,
    output wire         done,
    output wire         busy
);

    // ---- parameters ---------------------------------------------------------
    localparam integer N     = 16;   // ring degree
    localparam integer WIDTH = 9;    // ceil(log2(q + 1)) -- residues are 0..256

    // The six phases.  PH_IDLE is never stored: `run` low is idle.
    localparam [2:0] PH_NTT   = 3'd0;
    localparam [2:0] PH_POINT = 3'd1;
    localparam [2:0] PH_BREV  = 3'd2;
    localparam [2:0] PH_INTT  = 3'd3;
    localparam [2:0] PH_POST  = 3'd4;

    // ---- state --------------------------------------------------------------
    reg [WIDTH-1:0] pa [0:N-1];   // the working bank: a, then the transform
    reg [WIDTH-1:0] pb [0:N-1];   // b, then the pointwise product, then c
    reg [5:0]       cnt;          // the step counter inside a phase
    reg [2:0]       ph;           // which phase
    reg             run;          // a product is in progress
    reg             fin;          // it finished; pb holds the result

    // A start is accepted only when idle, exactly as in walkthroughs 11, 13
    // and 14.  `keep` again for coverage: unkept, `start & ~run` is inlined
    // into every one of the 288 coefficient registers' enable cones.
    wire load /* synthesis keep */;
    assign load = start & ~run;

    // ---- the phase counter --------------------------------------------------
    // NTT runs 64 steps, INTT 32, the three scalar phases 16.  Split into three
    // kept terms so that the terminal-count test is six inputs and not nine.
    wire cnt_eq15 /* synthesis keep */;
    wire cnt_eq31 /* synthesis keep */;
    wire cnt_eq63 /* synthesis keep */;
    assign cnt_eq15 = (cnt[3:0] == 4'hF);
    assign cnt_eq31 = cnt_eq15 & cnt[4];
    assign cnt_eq63 = cnt_eq31 & cnt[5];

    wire last_step /* synthesis keep */;
    assign last_step = (ph == PH_NTT)  ? cnt_eq63
                     : (ph == PH_INTT) ? cnt_eq31
                                       : cnt_eq15;

    wire last_phase /* synthesis keep */;
    assign last_phase = (ph == PH_POST) & last_step;

    // A butterfly phase: the only two that read two coefficients and write two.
    wire bf /* synthesis keep */;
    assign bf = (ph == PH_NTT) | (ph == PH_INTT);

    // ---- the address generator: where the transform's shape actually lives --
    // Step `cnt` of a butterfly phase is stage s = cnt[4:3] and position
    // m = cnt[2:0].  Stage s has 2^s groups of len = 8 >> s butterflies, and
    // the pair it touches is (j, j + len) where j is m with a **zero inserted
    // at bit position 3 - s** and the partner is the same index with a one
    // there.  That insertion is the entire loop nest of a textbook iterative
    // NTT, rendered as four wires.
    wire [1:0] stg;
    wire [2:0] mpos;
    wire [3:0] idx;
    assign stg  = cnt[4:3];
    assign mpos = cnt[2:0];
    assign idx  = cnt[3:0];

    reg [3:0] bj_r;
    always @* begin
        case (stg)
            2'd0: bj_r = {1'b0, mpos};
            2'd1: bj_r = {mpos[2], 1'b0, mpos[1:0]};
            2'd2: bj_r = {mpos[2:1], 1'b0, mpos[0]};
            default: bj_r = {mpos, 1'b0};
        endcase
    end

    reg [3:0] lenmask_r;
    always @* begin
        case (stg)
            2'd0: lenmask_r = 4'b1000;
            2'd1: lenmask_r = 4'b0100;
            2'd2: lenmask_r = 4'b0010;
            default: lenmask_r = 4'b0001;
        endcase
    end

    wire [3:0] bj /* synthesis keep */;
    wire [3:0] bk /* synthesis keep */;
    assign bj = bj_r;
    assign bk = bj_r | lenmask_r;

    // The bit-reversal permutation, which costs exactly nothing: it is which
    // wire is called what.
    wire [3:0] brv_idx;
    assign brv_idx = {idx[0], idx[1], idx[2], idx[3]};

    // ---- read and write addressing ------------------------------------------
    wire [3:0] ra /* synthesis keep */;
    wire [3:0] rb /* synthesis keep */;
    assign ra = bf ? bj : idx;
    assign rb = bf ? bk : idx;

    wire [3:0] wsum_addr /* synthesis keep */;
    wire [3:0] wdif_addr /* synthesis keep */;
    assign wsum_addr = bf ? bj
                     : ((ph == PH_BREV) | (ph == PH_POST)) ? brv_idx
                                                           : idx;
    assign wdif_addr = bk;

    // Which of the two banks each port talks to.  `cnt[5]` is the NTT phase's
    // "which polynomial am I transforming" bit: steps 0..31 are pa, 32..63 pb.
    wire arr_b /* synthesis keep */;
    assign arr_b = cnt[5];

    wire u_from_b /* synthesis keep */;
    wire v_from_b /* synthesis keep */;
    wire w_to_b   /* synthesis keep */;
    assign u_from_b = (ph == PH_NTT) & arr_b;
    assign v_from_b = (ph == PH_NTT) ? arr_b
                                     : ((ph == PH_POINT) | (ph == PH_BREV));
    assign w_to_b   = (ph == PH_NTT) ? arr_b
                                     : ((ph == PH_POINT) | (ph == PH_POST));

    // ---- the four read ports ------------------------------------------------
    // Four 16-to-1 multiplexers, nine bits wide, each written out as four
    // 4-to-1 cones and a fifth on top.  Left as `pa[ra]`, Quartus builds the
    // top level out of the ALM's fracturable seven-input mode -- a real
    // tennm_lcell_comb configuration, but not one hal_agilex models, so the
    // export would be reported `unsupported`.  Spelling the tree out and
    // keeping the middle vector holds every cone at six inputs: four data bits
    // and two select bits.
    wire [4*WIDTH-1:0] pa_ra_q /* synthesis keep */;
    wire [4*WIDTH-1:0] pa_rb_q /* synthesis keep */;
    wire [4*WIDTH-1:0] pb_ra_q /* synthesis keep */;
    wire [4*WIDTH-1:0] pb_rb_q /* synthesis keep */;

    genvar gq;
    generate
        for (gq = 0; gq < 4; gq = gq + 1) begin : quad
            assign pa_ra_q[WIDTH*gq +: WIDTH] = pa[4 * gq + ra[1:0]];
            assign pa_rb_q[WIDTH*gq +: WIDTH] = pa[4 * gq + rb[1:0]];
            assign pb_ra_q[WIDTH*gq +: WIDTH] = pb[4 * gq + ra[1:0]];
            assign pb_rb_q[WIDTH*gq +: WIDTH] = pb[4 * gq + rb[1:0]];
        end
    endgenerate

    wire [WIDTH-1:0] pa_ra /* synthesis keep */;
    wire [WIDTH-1:0] pa_rb /* synthesis keep */;
    wire [WIDTH-1:0] pb_ra /* synthesis keep */;
    wire [WIDTH-1:0] pb_rb /* synthesis keep */;
    assign pa_ra = pa_ra_q[WIDTH*ra[3:2] +: WIDTH];
    assign pa_rb = pa_rb_q[WIDTH*rb[3:2] +: WIDTH];
    assign pb_ra = pb_ra_q[WIDTH*ra[3:2] +: WIDTH];
    assign pb_rb = pb_rb_q[WIDTH*rb[3:2] +: WIDTH];

    // ---- the twiddle tables -------------------------------------------------
    // Two 32-entry tables of nine bits over the five low counter bits -- one
    // ALM per output bit per table -- and a two-to-one select on top.  Written
    // as one 64-entry table over {inv_ph, cnt[4:0]} instead, Quartus shares a
    // sub-term between bits and spills one of them into the fracturable
    // seven-input mode; two tables plus a select keeps every cone at five
    // inputs and the select at three.
    //
    // The forward entries are the *merged* twiddles psi^brv4(2^s + g): the
    // psi^i pre-twist of the negacyclic transform, folded into the butterfly
    // constants so it costs no pass of its own.  The inverse entries are the
    // plain cyclic twiddles omega^-brv3(g).  Recovering these 64 constants out
    // of the LUT masks is half of what the walkthrough does.
    wire inv_ph /* synthesis keep */;
    wire post_ph /* synthesis keep */;
    assign inv_ph  = (ph == PH_INTT);
    assign post_ph = (ph == PH_POST);

    reg [WIDTH-1:0] tw_fwd_r;
    always @* begin
        case (cnt[4:0])
            5'd0 : tw_fwd_r = 9'd16;
            5'd1 : tw_fwd_r = 9'd16;
            5'd2 : tw_fwd_r = 9'd16;
            5'd3 : tw_fwd_r = 9'd16;
            5'd4 : tw_fwd_r = 9'd16;
            5'd5 : tw_fwd_r = 9'd16;
            5'd6 : tw_fwd_r = 9'd16;
            5'd7 : tw_fwd_r = 9'd16;
            5'd8 : tw_fwd_r = 9'd253;
            5'd9 : tw_fwd_r = 9'd253;
            5'd10: tw_fwd_r = 9'd253;
            5'd11: tw_fwd_r = 9'd253;
            5'd12: tw_fwd_r = 9'd193;
            5'd13: tw_fwd_r = 9'd193;
            5'd14: tw_fwd_r = 9'd193;
            5'd15: tw_fwd_r = 9'd193;
            5'd16: tw_fwd_r = 9'd225;
            5'd17: tw_fwd_r = 9'd225;
            5'd18: tw_fwd_r = 9'd2;
            5'd19: tw_fwd_r = 9'd2;
            5'd20: tw_fwd_r = 9'd128;
            5'd21: tw_fwd_r = 9'd128;
            5'd22: tw_fwd_r = 9'd249;
            5'd23: tw_fwd_r = 9'd249;
            5'd24: tw_fwd_r = 9'd15;
            5'd25: tw_fwd_r = 9'd240;
            5'd26: tw_fwd_r = 9'd197;
            5'd27: tw_fwd_r = 9'd68;
            5'd28: tw_fwd_r = 9'd34;
            5'd29: tw_fwd_r = 9'd30;
            5'd30: tw_fwd_r = 9'd121;
            default: tw_fwd_r = 9'd137;
        endcase
    end

    reg [WIDTH-1:0] tw_inv_r;
    always @* begin
        case (cnt[4:0])
            5'd12: tw_inv_r = 9'd241;
            5'd13: tw_inv_r = 9'd241;
            5'd14: tw_inv_r = 9'd241;
            5'd15: tw_inv_r = 9'd241;
            5'd18: tw_inv_r = 9'd241;
            5'd19: tw_inv_r = 9'd241;
            5'd20: tw_inv_r = 9'd64;
            5'd21: tw_inv_r = 9'd64;
            5'd22: tw_inv_r = 9'd4;
            5'd23: tw_inv_r = 9'd4;
            5'd25: tw_inv_r = 9'd241;
            5'd26: tw_inv_r = 9'd64;
            5'd27: tw_inv_r = 9'd4;
            5'd28: tw_inv_r = 9'd8;
            5'd29: tw_inv_r = 9'd129;
            5'd30: tw_inv_r = 9'd255;
            5'd31: tw_inv_r = 9'd32;
            default: tw_inv_r = 9'd1;
        endcase
    end

    wire [WIDTH-1:0] tw_fwd_w /* synthesis keep */;
    wire [WIDTH-1:0] tw_inv_w /* synthesis keep */;
    assign tw_fwd_w = tw_fwd_r;
    assign tw_inv_w = tw_inv_r;

    // The scalar-phase constant: n^-1 * psi^-brv4(j) during POST, and a plain
    // one during BREV and POINT (BREV's "copy" is a multiply by one, which is
    // how a bit-reversal pass reuses the butterfly's multiplier).
    reg [WIDTH-1:0] tw_sc_r;
    always @* begin
        if (post_ph) begin
            case (idx)
                4'd0 : tw_sc_r = 9'd241;
                4'd1 : tw_sc_r = 9'd256;
                4'd2 : tw_sc_r = 9'd4;
                4'd3 : tw_sc_r = 9'd193;
                4'd4 : tw_sc_r = 9'd129;
                4'd5 : tw_sc_r = 9'd249;
                4'd6 : tw_sc_r = 9'd32;
                4'd7 : tw_sc_r = 9'd2;
                4'd8 : tw_sc_r = 9'd136;
                4'd9 : tw_sc_r = 9'd137;
                4'd10: tw_sc_r = 9'd223;
                4'd11: tw_sc_r = 9'd30;
                4'd12: tw_sc_r = 9'd60;
                4'd13: tw_sc_r = 9'd68;
                4'd14: tw_sc_r = 9'd242;
                default: tw_sc_r = 9'd240;
            endcase
        end else begin
            tw_sc_r = 9'd1;
        end
    end

    wire [WIDTH-1:0] tw_bf_w /* synthesis keep */;
    wire [WIDTH-1:0] tw_sc_w /* synthesis keep */;
    assign tw_bf_w = inv_ph ? tw_inv_w : tw_fwd_w;
    assign tw_sc_w = tw_sc_r;

    // ---- the butterfly's three operands -------------------------------------
    // u is the "top" coefficient of the pair, and is forced to zero outside a
    // butterfly phase so that the scalar phases are the *same* datapath with
    // u = 0: sum = 0 + w*v is a plain modular multiply.
    // v is the "bottom" coefficient.  w is the twiddle -- except in POINT,
    // where it is pa[i] and the "twiddle" is the other polynomial.
    wire [WIDTH-1:0] u /* synthesis keep */;
    wire [WIDTH-1:0] v /* synthesis keep */;
    wire [WIDTH-1:0] w /* synthesis keep */;
    assign u = bf ? (u_from_b ? pb_ra : pa_ra) : {WIDTH{1'b0}};
    assign v = v_from_b ? pb_rb : pa_rb;
    assign w = (ph == PH_POINT) ? pa_ra : (bf ? tw_bf_w : tw_sc_w);

    // ---- the modular multiplier ---------------------------------------------
    // Both operands are canonical residues in [0, 256], so the product is at
    // most 256*256 = 65536 and seventeen bits are enough.
    wire [16:0] prod /* synthesis keep */;
    assign prod = w * v;

    // 256 = -1 (mod 257): the reduction is Pl - Ph, corrected once.
    wire [9:0] mred /* synthesis keep */;
    wire [8:0] mfix /* synthesis keep */;
    wire [WIDTH-1:0] t /* synthesis keep */;
    assign mred = {2'b00, prod[7:0]} - {1'b0, prod[16:8]};
    assign mfix = mred[8:0] + 9'd257;
    assign t    = mred[9] ? mfix : mred[8:0];

    // ---- the butterfly ------------------------------------------------------
    // One adder and one subtracter over the *same* two operand vectors, each
    // followed by a conditional correction by q.  This is the shape
    // `hal_crypto ntt` looks for, and the constant 257 in the two corrections
    // is the modulus, sitting in the netlist as a carry chain's second operand.
    wire [9:0] sum_raw /* synthesis keep */;
    wire [9:0] sum_red /* synthesis keep */;
    wire [WIDTH-1:0] sum_mod /* synthesis keep */;
    assign sum_raw = {1'b0, u} + {1'b0, t};
    assign sum_red = sum_raw - 10'd257;
    assign sum_mod = sum_red[9] ? sum_raw[8:0] : sum_red[8:0];

    wire [9:0] dif_raw /* synthesis keep */;
    wire [8:0] dif_fix /* synthesis keep */;
    wire [WIDTH-1:0] dif_mod /* synthesis keep */;
    assign dif_raw = {1'b0, u} - {1'b0, t};
    assign dif_fix = dif_raw[8:0] + 9'd257;
    assign dif_mod = dif_raw[9] ? dif_fix : dif_raw[8:0];

    // ---- the write ports ----------------------------------------------------
    wire we_sum /* synthesis keep */;
    wire we_dif /* synthesis keep */;
    assign we_sum = run;             // every phase writes one result
    assign we_dif = run & bf;        // only a butterfly writes two

    genvar gi;
    generate
        for (gi = 0; gi < N; gi = gi + 1) begin : coeff
            wire hit_a_sum /* synthesis keep */;
            wire hit_a_dif /* synthesis keep */;
            wire hit_b_sum /* synthesis keep */;
            wire hit_b_dif /* synthesis keep */;
            assign hit_a_sum = we_sum & ~w_to_b & (wsum_addr == gi[3:0]);
            assign hit_a_dif = we_dif & ~w_to_b & (wdif_addr == gi[3:0]);
            assign hit_b_sum = we_sum &  w_to_b & (wsum_addr == gi[3:0]);
            assign hit_b_dif = we_dif &  w_to_b & (wdif_addr == gi[3:0]);

            always @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    pa[gi] <= {WIDTH{1'b0}};
                    pb[gi] <= {WIDTH{1'b0}};
                end else if (load) begin
                    pa[gi] <= {1'b0, a_in[8*gi +: 8]};
                    pb[gi] <= {1'b0, b_in[8*gi +: 8]};
                end else begin
                    if (hit_a_sum | hit_a_dif)
                        pa[gi] <= hit_a_dif ? dif_mod : sum_mod;
                    if (hit_b_sum | hit_b_dif)
                        pb[gi] <= hit_b_dif ? dif_mod : sum_mod;
                end
            end

            assign c_out[WIDTH*gi +: WIDTH] = pb[gi];
        end
    endgenerate

    // ---- the control register -----------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 6'd0;
            ph  <= PH_NTT;
            run <= 1'b0;
            fin <= 1'b0;
        end else if (load) begin
            cnt <= 6'd0;
            ph  <= PH_NTT;
            run <= 1'b1;
            fin <= 1'b0;
        end else if (run) begin
            cnt <= last_step ? 6'd0 : (cnt + 6'd1);
            if (last_step) ph <= ph + 3'd1;
            if (last_phase) begin
                run <= 1'b0;
                fin <= 1'b1;
            end
        end
    end

    assign done = fin;
    assign busy = run;

endmodule

`default_nettype wire
