// hal_semantic_diff fixture -- build A ("base")
//
// A 2-to-4 address decoder with an enable, a registered select bus and a
// combinational HIT output, against plugins/gate_libraries/definitions/example_library.hgl.
//
// Behaviour (ground truth, see ground_truth.json):
//   s0 = ~A1 & ~A0      g0 = s0 & EN      SEL0 = reg(g0)
//   s1 = ~A1 &  A0      g1 = s1 & EN      SEL1 = reg(g1)
//   s2 =  A1 & ~A0      g2 = s2 & EN      SEL2 = reg(g2)
//   s3 =  A1 &  A0      g3 = s3 & EN      SEL3 = reg(g3)
//   HIT = g0 | g1 | g2 | g3  ==  EN      (the decoder is complete)
//
// The SEL* outputs are register outputs on purpose: that is the common case,
// and it is what makes the *register inputs* -- not the output pins -- the
// observation points that carry the information.

module addr_decoder (
    A0, A1, EN, CLK, RST, SEL0, SEL1, SEL2, SEL3, HIT
);
    input A0, A1, EN, CLK, RST;
    output SEL0, SEL1, SEL2, SEL3, HIT;

    wire one;
    wire n_a0, n_a1;
    wire s0, s1, s2, s3;
    wire g0, g1, g2, g3;

    VCC vcc_inst ( .O (one) );

    INV inv_a0 ( .I (A0), .O (n_a0) );
    INV inv_a1 ( .I (A1), .O (n_a1) );

    AND2 dec0 ( .I0 (n_a1), .I1 (n_a0), .O (s0) );
    AND2 dec1 ( .I0 (n_a1), .I1 (A0),   .O (s1) );
    AND2 dec2 ( .I0 (A1),   .I1 (n_a0), .O (s2) );
    AND2 dec3 ( .I0 (A1),   .I1 (A0),   .O (s3) );

    AND2 en0 ( .I0 (s0), .I1 (EN), .O (g0) );
    AND2 en1 ( .I0 (s1), .I1 (EN), .O (g1) );
    AND2 en2 ( .I0 (s2), .I1 (EN), .O (g2) );
    AND2 en3 ( .I0 (s3), .I1 (EN), .O (g3) );

    OR4 hit_or ( .I0 (g0), .I1 (g1), .I2 (g2), .I3 (g3), .O (HIT) );

    FFR sel_reg_0 ( .C (CLK), .CE (one), .D (g0), .R (RST), .Q (SEL0) );
    FFR sel_reg_1 ( .C (CLK), .CE (one), .D (g1), .R (RST), .Q (SEL1) );
    FFR sel_reg_2 ( .C (CLK), .CE (one), .D (g2), .R (RST), .Q (SEL2) );
    FFR sel_reg_3 ( .C (CLK), .CE (one), .D (g3), .R (RST), .Q (SEL3) );

endmodule
