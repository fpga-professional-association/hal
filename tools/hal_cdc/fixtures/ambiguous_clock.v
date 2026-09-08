// Two generated clocks with deliberately different outcomes.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//   MUX  : I0, I1, S (select) -> O
//   AND2 : I0, I1 -> O
//
//   mux_clk   = MUX(clk_a, clk_b, sel)  -> two declared clocks reach the same
//               generated clock. Which one is live depends on 'sel', which this
//               structural pass does not evaluate, so mux_reg's domain is
//               UNKNOWN and none of its inputs are reported as crossings.
//   gated_clk = AND2(clk_a, en)         -> exactly one declared clock reaches
//               it, so gated_reg is screened as domain clk_a with the
//               resolution flagged 'derived' and 'ambiguous': the enable's
//               timing is not analysed.
module ambiguous_clock (
  clk_a,
  clk_b,
  sel,
  en,
  din,
  dout_mux,
  dout_gated
) ;
  input clk_a ;
  input clk_b ;
  input sel ;
  input en ;
  input din ;
  output dout_mux ;
  output dout_gated ;
  wire vdd ;
  wire mux_clk ;
  wire gated_clk ;
  wire mux_q ;
  wire gated_q ;

  VCC tie_high (
    .O (vdd )
  ) ;

  MUX clk_mux (
    .I0 (clk_a ),
    .I1 (clk_b ),
    .S (sel ),
    .O (mux_clk )
  ) ;

  AND2 clk_gate (
    .I0 (clk_a ),
    .I1 (en ),
    .O (gated_clk )
  ) ;

  FF mux_reg (
    .C (mux_clk ),
    .CE (vdd ),
    .D (din ),
    .Q (mux_q )
  ) ;

  FF gated_reg (
    .C (gated_clk ),
    .CE (vdd ),
    .D (din ),
    .Q (gated_q )
  ) ;

  BUF out_mux (
    .I (mux_q ),
    .O (dout_mux )
  ) ;

  BUF out_gated (
    .I (gated_q ),
    .O (dout_gated )
  ) ;
endmodule
