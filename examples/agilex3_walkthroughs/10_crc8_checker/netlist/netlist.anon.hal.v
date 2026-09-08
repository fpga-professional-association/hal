// Structurally identical to netlist.hal.v with every design-specific identifier
// removed, internal vectors split into unrelated scalars and both instance and
// net numbering scrambled (see anonymize.py).  This is the file the walkthrough
// analyses: what a netlist looks like when the vendor tool did not hand you the
// RTL names.
// Load with the AGILEX_TENNM gate library.

module top (port_o0, port_o1, port_i0, port_i1, port_i2, port_i3);
output [7:0] port_o0;
output port_o1;
input port_i0;
input port_i1;
input port_i2;
input port_i3;
wire n5;
wire n12;
wire n6;
wire n0;
wire n7;
wire n1;
wire n8;
wire n2;
wire devclrn;
wire devoe;
wire devpor;
wire n9 ;
wire gnd;
wire n3 ;
wire n10 ;
wire n4 ;
wire n11 ;
wire unknown;
wire vcc;

assign gnd = 1'b0;
assign vcc = 1'b1;
// dropped: assign unknown = 1'bx;  (declared by Quartus, read by nothing)
assign devclrn = 1'b1;
assign devpor = 1'b1;
assign devoe = 1'b1;
assign port_o1 = n11 ;
assign port_o0[0] = n2;
assign port_o0[1] = n6;
assign port_o0[2] = n1;
assign port_o0[3] = n5;
assign port_o0[4] = n0;
assign port_o0[5] = n8;
assign port_o0[6] = n12;
assign port_o0[7] = n7;

tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u3  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n9 ), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n2), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u5  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n3 ), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n6), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u7  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n10 ), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n1), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u9  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n1), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n5), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u11  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n5), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n0), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u0  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n0), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n8), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u2  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n8), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n12), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_ff #(.is_wysiwyg ("true"), .power_up ("dont_care")) u4  (.aload (gnd), .asdata (vcc), .clk (port_i1), .clrn (port_i2), .d (n12), .devclrn (devclrn), .devpor (devpor), .ena (port_i3), .q (n7), .sclr (gnd), .sclr1 (gnd), .sload (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h0001000100010001), .shared_arith ("off")) u6  (.cin (gnd), .combout (n4 ), .dataa (n2), .datab (n6), .datac (n1), .datad (n5), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h0001000000010000), .shared_arith ("off")) u8 (.cin (gnd), .combout (n11 ), .dataa (n0), .datab (n8), .datac (n12), .datad (n7), .datae (n4 ), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h6666666666666666), .shared_arith ("off")) u10 (.cin (gnd), .combout (n9 ), .dataa (n7), .datab (port_i0), .datac (gnd), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h9696969696969696), .shared_arith ("off")) u12 (.cin (gnd), .combout (n3 ), .dataa (n2), .datab (n7), .datac (port_i0), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));
tennm_lcell_comb #(.extended_lut ("off"), .lut_mask (64'h9696969696969696), .shared_arith ("off")) u1 (.cin (gnd), .combout (n10 ), .dataa (n6), .datab (n7), .datac (port_i0), .datad (gnd), .datae (gnd), .dataf (gnd), .datag (gnd), .datah (gnd), .sharein (gnd));

endmodule
