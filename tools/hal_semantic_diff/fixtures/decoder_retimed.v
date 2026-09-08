// hal_semantic_diff fixture -- build B''' ("retimed", a REJECTED input)
//
// decoder_base.v with the enable gating moved from before the registers to
// after them: the registers now hold the ungated decode, and EN gates the
// register outputs. Over a full clock cycle the SEL* pins behave the same as
// in decoder_base.v once EN has settled -- a retiming tool would call this a
// legal transformation.
//
// This tool does not, and that is deliberate. Its model assumes matched
// registers hold matched state (`matched-state-correspondence`), which
// retiming breaks by construction: sel_reg_0 in this build holds s0, and in
// decoder_base.v it holds s0 & EN. So:
//
// Ground truth against decoder_base.v:
//   * all four sel_reg_N.D points differ (difference function sN & ~EN)
//   * all four SEL* output pins differ (difference sel_reg_N_Q & ~EN)
//   * HIT differs
//   * the clock, enable and reset pins of the registers are still equivalent
//
// The right reading is "this transformation is outside the model", and the
// README says so; the wrong reading -- "the tool proved a retimed build
// broken" -- is exactly what the assumptions list on every finding prevents.

module addr_decoder (
    A0, A1, EN, CLK, RST, SEL0, SEL1, SEL2, SEL3, HIT
);
    input A0, A1, EN, CLK, RST;
    output SEL0, SEL1, SEL2, SEL3, HIT;

    wire one;
    wire n_a0, n_a1;
    wire s0, s1, s2, s3;
    wire q0, q1, q2, q3;

    VCC vcc_inst ( .O (one) );

    INV inv_a0 ( .I (A0), .O (n_a0) );
    INV inv_a1 ( .I (A1), .O (n_a1) );

    AND2 dec0 ( .I0 (n_a1), .I1 (n_a0), .O (s0) );
    AND2 dec1 ( .I0 (n_a1), .I1 (A0),   .O (s1) );
    AND2 dec2 ( .I0 (A1),   .I1 (n_a0), .O (s2) );
    AND2 dec3 ( .I0 (A1),   .I1 (A0),   .O (s3) );

    // the registers now hold the UNGATED decode
    FFR sel_reg_0 ( .C (CLK), .CE (one), .D (s0), .R (RST), .Q (q0) );
    FFR sel_reg_1 ( .C (CLK), .CE (one), .D (s1), .R (RST), .Q (q1) );
    FFR sel_reg_2 ( .C (CLK), .CE (one), .D (s2), .R (RST), .Q (q2) );
    FFR sel_reg_3 ( .C (CLK), .CE (one), .D (s3), .R (RST), .Q (q3) );

    // ... and the enable gates the register outputs instead
    AND2 en0 ( .I0 (q0), .I1 (EN), .O (SEL0) );
    AND2 en1 ( .I0 (q1), .I1 (EN), .O (SEL1) );
    AND2 en2 ( .I0 (q2), .I1 (EN), .O (SEL2) );
    AND2 en3 ( .I0 (q3), .I1 (EN), .O (SEL3) );

    OR4 hit_or ( .I0 (SEL0), .I1 (SEL1), .I2 (SEL2), .I3 (SEL3), .O (HIT) );

endmodule
