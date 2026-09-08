// hal_semantic_diff fixture -- build B ("equivalent rewrite")
//
// The same behaviour as decoder_base.v, written differently everywhere it can be:
//
//   * the AND decode is De Morgan'd:  sN = ~(x | y)  instead of  ~x & ~y
//   * the enable gating is a MUX with a hard 0 on I0:
//       O = (~S & I0) | (S & I1) = (~EN & 0) | (EN & sN) = EN & sN
//   * the HIT reduction is a two-level OR2 tree instead of one OR4
//
// Not one combinational gate instance survives with the same type and the same
// fan-in, so a structural diff reports the whole cone as changed. The register
// names are identical, which is what the correspondence rests on.
//
// Ground truth: behaviourally identical to decoder_base.v at every observation
// point.

module addr_decoder (
    A0, A1, EN, CLK, RST, SEL0, SEL1, SEL2, SEL3, HIT
);
    input A0, A1, EN, CLK, RST;
    output SEL0, SEL1, SEL2, SEL3, HIT;

    wire one, zero;
    wire n_a0, n_a1;
    wire t0, t1, t2, t3;
    wire s0, s1, s2, s3;
    wire g0, g1, g2, g3;
    wire h01, h23;

    VCC vcc_inst ( .O (one) );
    GND gnd_inst ( .O (zero) );

    INV inv_a0 ( .I (A0), .O (n_a0) );
    INV inv_a1 ( .I (A1), .O (n_a1) );

    // sN = ~(...) : De Morgan of the AND decode in decoder_base.v
    OR2 nor0_or ( .I0 (A1),   .I1 (A0),   .O (t0) );
    INV nor0_inv ( .I (t0), .O (s0) );                 // ~(A1 | A0)   = ~A1 & ~A0
    OR2 nor1_or ( .I0 (A1),   .I1 (n_a0), .O (t1) );
    INV nor1_inv ( .I (t1), .O (s1) );                 // ~(A1 | ~A0)  = ~A1 &  A0
    OR2 nor2_or ( .I0 (n_a1), .I1 (A0),   .O (t2) );
    INV nor2_inv ( .I (t2), .O (s2) );                 // ~(~A1 | A0)  =  A1 & ~A0
    OR2 nor3_or ( .I0 (n_a1), .I1 (n_a0), .O (t3) );
    INV nor3_inv ( .I (t3), .O (s3) );                 // ~(~A1 | ~A0) =  A1 &  A0

    MUX en0 ( .I0 (zero), .I1 (s0), .S (EN), .O (g0) );
    MUX en1 ( .I0 (zero), .I1 (s1), .S (EN), .O (g1) );
    MUX en2 ( .I0 (zero), .I1 (s2), .S (EN), .O (g2) );
    MUX en3 ( .I0 (zero), .I1 (s3), .S (EN), .O (g3) );

    OR2 hit_01 ( .I0 (g0), .I1 (g1), .O (h01) );
    OR2 hit_23 ( .I0 (g2), .I1 (g3), .O (h23) );
    OR2 hit_or ( .I0 (h01), .I1 (h23), .O (HIT) );

    FFR sel_reg_0 ( .C (CLK), .CE (one), .D (g0), .R (RST), .Q (SEL0) );
    FFR sel_reg_1 ( .C (CLK), .CE (one), .D (g1), .R (RST), .Q (SEL1) );
    FFR sel_reg_2 ( .C (CLK), .CE (one), .D (g2), .R (RST), .Q (SEL2) );
    FFR sel_reg_3 ( .C (CLK), .CE (one), .D (g3), .R (RST), .Q (SEL3) );

endmodule
