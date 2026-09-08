// One asynchronous reset released two different ways.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//   FFR : C (clock), CE (enable), D (data), R (reset) -> Q, clear_on = R
//   INV : I -> O
//   VCC : O = 1
//
// clk_a is protected by the textbook reset synchroniser: two FFRs clocked by
// clk_a, both asynchronously cleared by arst, with a constant one shifted
// through them. rst_a_inv turns the "released" level back into the active-high
// reset the register bank expects.
//
// clk_b gets the raw asynchronous reset straight onto b_reg's R pin: asserting
// it is fine, releasing it is not synchronised to clk_b.
module reset_release (
  clk_a,
  clk_b,
  arst,
  din_a,
  din_b,
  dout_a,
  dout_b
) ;
  input clk_a ;
  input clk_b ;
  input arst ;
  input din_a ;
  input din_b ;
  output dout_a ;
  output dout_b ;
  wire vdd ;
  wire sync_stage1 ;
  wire sync_stage2 ;
  wire rst_a_sync ;
  wire a_q ;
  wire b_q ;

  VCC tie_high (
    .O (vdd )
  ) ;

  // ---- reset synchroniser for clk_a -----------------------------------
  FFR rst_sync_reg1 (
    .C (clk_a ),
    .CE (vdd ),
    .D (vdd ),
    .R (arst ),
    .Q (sync_stage1 )
  ) ;

  FFR rst_sync_reg2 (
    .C (clk_a ),
    .CE (vdd ),
    .D (sync_stage1 ),
    .R (arst ),
    .Q (sync_stage2 )
  ) ;

  INV rst_a_inv (
    .I (sync_stage2 ),
    .O (rst_a_sync )
  ) ;

  FFR a_reg (
    .C (clk_a ),
    .CE (vdd ),
    .D (din_a ),
    .R (rst_a_sync ),
    .Q (a_q )
  ) ;

  // ---- clk_b takes the raw asynchronous reset -------------------------
  FFR b_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (din_b ),
    .R (arst ),
    .Q (b_q )
  ) ;

  BUF out_a (
    .I (a_q ),
    .O (dout_a )
  ) ;

  BUF out_b (
    .I (b_q ),
    .O (dout_b )
  ) ;
endmodule
