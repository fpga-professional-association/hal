// hal_semantic_diff fixture -- build B' ("changed address decoder")
//
// Identical to decoder_rewrite.v except for two swapped decode terms:
//
//     nor2_or:  ~A1 | A0   ->  ~A1 | ~A0     so  s2 = A1 & ~A0  ->  A1 &  A0
//     nor3_or:  ~A1 | ~A0  ->  ~A1 |  A0     so  s3 = A1 &  A0  ->  A1 & ~A0
//
// SEL2 and SEL3 now decode each other's address; everything else is untouched.
// This is the realistic regression: an address decoder that still looks right,
// still passes any "does it toggle" check, and is wrong for two addresses.
//
// Ground truth against decoder_base.v / decoder_rewrite.v:
//   * sel_reg_2.D and sel_reg_3.D differ. The difference function of each is
//     exactly  GLOBAL_IN_EN & GLOBAL_IN_A1,  so EVERY counterexample has
//     EN = 1 and A1 = 1 (and A0 free) -- which is what the smoke test asserts.
//   * HIT does NOT differ: it is the OR of all four decode terms, and swapping
//     two of them does not change the set. A checker that only looked at the
//     top-level outputs would report this build as equivalent.
//   * every other observation point is equivalent.

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

    OR2 nor0_or ( .I0 (A1),   .I1 (A0),   .O (t0) );
    INV nor0_inv ( .I (t0), .O (s0) );                 // ~A1 & ~A0   (unchanged)
    OR2 nor1_or ( .I0 (A1),   .I1 (n_a0), .O (t1) );
    INV nor1_inv ( .I (t1), .O (s1) );                 // ~A1 &  A0   (unchanged)
    OR2 nor2_or ( .I0 (n_a1), .I1 (n_a0), .O (t2) );
    INV nor2_inv ( .I (t2), .O (s2) );                 //  A1 &  A0   (CHANGED)
    OR2 nor3_or ( .I0 (n_a1), .I1 (A0),   .O (t3) );
    INV nor3_inv ( .I (t3), .O (s3) );                 //  A1 & ~A0   (CHANGED)

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
