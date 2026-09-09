// Anonymized netlist: identical structure to netlist.hal.v with every
// design-derived identifier replaced.  Produced by anonymize.py.
// Load with plugins/gate_libraries/definitions/AGILEX_TENNM.hgl.
module top (o0, o1, o2, i0, i1, i2);
output o0;
output o1;
output o2;
input i0;
input i1;
input i2;
wire devclrn;
wire devoe;
wire devpor;
wire gnd;
wire n1 ;
wire n2 ;
wire n3 ;
wire n4 ;
wire n5 ;
wire n6 ;
wire n7 ;
wire n8 ;
wire n9 ;
wire n10 ;
wire n11 ;
wire n12 ;
wire n13 ;
wire n14 ;
wire n15 ;
wire n16 ;
wire n17 ;
wire n18;
wire n19;
wire n20;
wire n21;
wire unknown;
wire vcc;
wire n22 ;

assign gnd = 1'b0;
assign vcc = 1'b1;
assign devclrn = 1'b1;
assign devpor = 1'b1;
assign devoe = 1'b1;
assign o0 = n8 ;
assign o2 = n9 ;
assign o1 = n22 ;

tennm_ff #(.is_wysiwyg ("true"), .power_up ("low")) g1  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n14 ), .devclrn (devclrn), .devpor (devpor), .ena (vcc), .q (n12 ), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("low")) g2  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n15 ), .devclrn (devclrn), .devpor (devpor), .ena (vcc), .q (n10 ), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hDDDDDDDDDDDDDDDD), .shared_arith ("off")) g3  (.cin (gnd), .combout (n8 ), .dataa (n12 ), .datab (n10 ), .datac (gnd), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("low")) g4  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n16 ), .devclrn (devclrn), .devpor (devpor), .ena (vcc), .q (n13 ), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hEEEEEEEEEEEEEEEE), .shared_arith ("off")) g5  (.cin (gnd), .combout (n22 ), .dataa (n10 ), .datab (n13 ), .datac (gnd), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("low")) g6  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n17 ), .devclrn (devclrn), .devpor (devpor), .ena (vcc), .q (n9 ), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) g7  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n3 ), .devclrn (devclrn), .devpor (devpor), .ena (n1 ), .q (n21), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h2D2D2D2D2D2D2D2D), .shared_arith ("off")) g8  (.cin (gnd), .combout (n2 ), .dataa (n12 ), .datab (n9 ), .datac (n21), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) g9  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n4 ), .devclrn (devclrn), .devpor (devpor), .ena (n1 ), .q (n18), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) g10  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n5 ), .devclrn (devclrn), .devpor (devpor), .ena (n1 ), .q (n19), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) g11  (.aload (gnd), .asdata (vcc), .clk (i1), .clrn (i2), .d (n6 ), .devclrn (devclrn), .devpor (devpor), .ena (n1 ), .q (n20), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h0002481000024810), .shared_arith ("off")) g12  (.cin (gnd), .combout (n7 ), .dataa (n12 ), .datab (n10 ), .datac (n18), .datad (n19), .datae (n20), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h0229022902290229), .shared_arith ("off")) g13  (.cin (gnd), .combout (n11 ), .dataa (n12 ), .datab (n10 ), .datac (n13 ), .datad (n9 ), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hAAA3AAAA00000000), .shared_arith ("off")) g14  (.cin (gnd), .combout (n14 ), .dataa (n12 ), .datab (n13 ), .datac (i0), .datad (n2 ), .datae (n7 ), .dataf (n11 ), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hCCC5CCCC00000000), .shared_arith ("off")) g15  (.cin (gnd), .combout (n15 ), .dataa (n12 ), .datab (n10 ), .datac (i0), .datad (n2 ), .datae (n7 ), .dataf (n11 ), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hAAACAAAA00000000), .shared_arith ("off")) g16  (.cin (gnd), .combout (n16 ), .dataa (n13 ), .datab (n9 ), .datac (i0), .datad (n2 ), .datae (n7 ), .dataf (n11 ), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'hCCCACCCC00000000), .shared_arith ("off")) g17  (.cin (gnd), .combout (n17 ), .dataa (n10 ), .datab (n9 ), .datac (i0), .datad (n2 ), .datae (n7 ), .dataf (n11 ), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h7F8000007F807F80), .shared_arith ("off")) g18  (.cin (gnd), .combout (n3 ), .dataa (n18), .datab (n19), .datac (n20), .datad (n21), .datae (n2 ), .dataf (n7 ), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h4545454545454545), .shared_arith ("off")) g19  (.cin (gnd), .combout (n4 ), .dataa (n18), .datab (n2 ), .datac (n7 ), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h6066606660666066), .shared_arith ("off")) g20  (.cin (gnd), .combout (n5 ), .dataa (n18), .datab (n19), .datac (n2 ), .datad (n7 ), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h7800787878007878), .shared_arith ("off")) g21  (.cin (gnd), .combout (n6 ), .dataa (n18), .datab (n19), .datac (n20), .datad (n2 ), .datae (n7 ), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h5555555555555555), .shared_arith ("off")) g22  (.cin (gnd), .combout (n1 ), .dataa (i0), .datab (gnd), .datac (gnd), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));

endmodule