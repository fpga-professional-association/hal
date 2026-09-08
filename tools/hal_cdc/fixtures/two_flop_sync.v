// A clean, recognised two-flop synchroniser.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//   FF  : C (clock), CE (enable), D (data) -> Q (state), next_state = (D & CE)
//   BUF : I -> O
//   VCC : O = 1  (drives the clock enables so the flip-flops are always enabled)
//
// din is declared (see declarations/two_flop_sync.json) as already synchronous
// to clk_a, so the only domain crossing in this netlist is src_q -> sync_meta_reg.
module two_flop_sync (
  clk_a,
  clk_b,
  din,
  dout
) ;
  input clk_a ;
  input clk_b ;
  input din ;
  output dout ;
  wire vdd ;
  wire src_q ;
  wire meta_q ;
  wire sync_q ;

  VCC tie_high (
    .O (vdd )
  ) ;

  // source domain: clk_a
  FF src_reg (
    .C (clk_a ),
    .CE (vdd ),
    .D (din ),
    .Q (src_q )
  ) ;

  // destination domain: clk_b -- first (metastable) stage, single load
  FF sync_meta_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (src_q ),
    .Q (meta_q )
  ) ;

  // second stage
  FF sync_out_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (meta_q ),
    .Q (sync_q )
  ) ;

  BUF out_buf (
    .I (sync_q ),
    .O (dout )
  ) ;
endmodule
