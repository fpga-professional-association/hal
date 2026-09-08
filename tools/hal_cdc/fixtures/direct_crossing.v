// Four unsafe crossings from clk_a into clk_b, one per failure mode.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//
//   single_reg : captured once and never re-registered  -> no second stage
//   meta_reg   : captured, re-registered, but meta_q also feeds dup_buf
//                -> the first stage drives two loads, so the two loads can
//                   resolve the same metastable event differently
//   logic_reg  : an XOR sits between the source domain and the capture
//                -> combinational logic in front of the first stage
//   en_reg     : a clk_a signal drives a clock enable in clk_b
//                -> a control pin is never a synchroniser's first stage
module direct_crossing (
  clk_a,
  clk_b,
  din,
  dout_a,
  dout_b,
  dout_c,
  dout_d
) ;
  input clk_a ;
  input clk_b ;
  input din ;
  output dout_a ;
  output dout_b ;
  output dout_c ;
  output dout_d ;
  wire vdd ;
  wire a_q ;
  wire a_q2 ;
  wire a_comb ;
  wire meta_q ;
  wire tail_q ;
  wire logic_q ;
  wire en_q ;
  wire single_q ;

  VCC tie_high (
    .O (vdd )
  ) ;

  // ---- source domain: clk_a -------------------------------------------
  FF a_reg (
    .C (clk_a ),
    .CE (vdd ),
    .D (din ),
    .Q (a_q )
  ) ;

  FF a_reg2 (
    .C (clk_a ),
    .CE (vdd ),
    .D (a_q ),
    .Q (a_q2 )
  ) ;

  XOR a_xor (
    .I0 (a_q ),
    .I1 (a_q2 ),
    .O (a_comb )
  ) ;

  // ---- destination domain: clk_b --------------------------------------
  // (1) captured once, never re-registered
  FF single_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (a_q2 ),
    .Q (single_q )
  ) ;

  // (2) two-stage chain, but the first stage fans out to two loads
  FF meta_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (a_q2 ),
    .Q (meta_q )
  ) ;

  FF tail_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (meta_q ),
    .Q (tail_q )
  ) ;

  BUF dup_buf (
    .I (meta_q ),
    .O (dout_a )
  ) ;

  // (3) combinational logic between the domains
  FF logic_reg (
    .C (clk_b ),
    .CE (vdd ),
    .D (a_comb ),
    .Q (logic_q )
  ) ;

  // (4) a clk_a signal on a clock-enable pin in clk_b
  FF en_reg (
    .C (clk_b ),
    .CE (a_q ),
    .D (tail_q ),
    .Q (en_q )
  ) ;

  BUF out_b (
    .I (logic_q ),
    .O (dout_b )
  ) ;

  BUF out_c (
    .I (en_q ),
    .O (dout_c )
  ) ;

  BUF out_d (
    .I (single_q ),
    .O (dout_d )
  ) ;
endmodule
