// A hand-written fixture for tools/hal_explain. See README.md in this directory
// for the design intent and accumulator_ground_truth.json for what each analysis
// is expected to recover from it.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//
// The point of this netlist is that no single analysis explains it. Four things
// share it on purpose:
//
//   acc_r0..acc_r3     a 4-bit accumulator register -- the flip-flops DANA
//                      groups into one candidate word-level register
//   a_*                a 4-bit ripple-carry adder feeding that register -- the
//                      cone module_identification verifies as an addition
//   cmp_*              an equality check against the constant 4'b1010 -- the
//                      cone module_identification verifies as a comparison
//   st_a / st_b        a 3-state controller -- the state register hal_fsm
//                      proposes and solve_fsm solves
//
// and two things exist so that the composed model has to admit what it does not
// know:
//
//   p_x0..p_x2         a parity tree over the data inputs. No analysis in the
//                      pipeline claims it, so it must survive into the block
//                      model as an unknown region rather than disappearing.
//   u_gnd / u_vcc      tie cells. They are reachable only through constant
//                      nets, so they cluster with nothing and stay visible as
//                      their own regions.
//
// Everything is written in the subset tools/hal_cdc/fixture_netlist.py reads
// (one module, scalar nets, named port connections, no parameters), so the whole
// composition is testable without a HAL build. HAL's verilog_parser remains the
// authority: tests/headless_smoke/explain_blocks_smoke.py runs the same
// assertions through it.

module accumulator_top (
  clk,
  i_en,
  i_go,
  i_fin,
  d0,
  d1,
  d2,
  d3,
  o_acc0,
  o_acc1,
  o_acc2,
  o_acc3,
  o_match,
  o_busy,
  o_par
 ) ;
  input clk ;
  input i_en ;
  input i_go ;
  input i_fin ;
  input d0 ;
  input d1 ;
  input d2 ;
  input d3 ;
  output o_acc0 ;
  output o_acc1 ;
  output o_acc2 ;
  output o_acc3 ;
  output o_match ;
  output o_busy ;
  output o_par ;

  wire const0 ;
  wire const1 ;
  wire s0 ;
  wire s1 ;
  wire s2 ;
  wire s3 ;
  wire c0 ;
  wire c1 ;
  wire c2 ;
  wire t1 ;
  wire t2 ;
  wire t3 ;
  wire p1 ;
  wire p2 ;
  wire g1 ;
  wire g2 ;
  wire cn0 ;
  wire cn2 ;
  wire q_b ;
  wire d_a ;
  wire d_b ;
  wire n_b ;
  wire n_fin ;
  wire m_a ;
  wire px0 ;
  wire px1 ;

  GND u_gnd (
    .O (const0 )
  ) ;
  VCC u_vcc (
    .O (const1 )
  ) ;

  // -- the adder: o_acc + d, ripple carry, no carry out ---------------------
  XOR a_x0 (
    .I0 (o_acc0 ),
    .I1 (d0 ),
    .O (s0 )
  ) ;
  AND2 a_c0 (
    .I0 (o_acc0 ),
    .I1 (d0 ),
    .O (c0 )
  ) ;
  XOR a_t1 (
    .I0 (o_acc1 ),
    .I1 (d1 ),
    .O (t1 )
  ) ;
  XOR a_x1 (
    .I0 (t1 ),
    .I1 (c0 ),
    .O (s1 )
  ) ;
  AND2 a_p1 (
    .I0 (o_acc1 ),
    .I1 (d1 ),
    .O (p1 )
  ) ;
  AND2 a_g1 (
    .I0 (t1 ),
    .I1 (c0 ),
    .O (g1 )
  ) ;
  OR2 a_o1 (
    .I0 (p1 ),
    .I1 (g1 ),
    .O (c1 )
  ) ;
  XOR a_t2 (
    .I0 (o_acc2 ),
    .I1 (d2 ),
    .O (t2 )
  ) ;
  XOR a_x2 (
    .I0 (t2 ),
    .I1 (c1 ),
    .O (s2 )
  ) ;
  AND2 a_p2 (
    .I0 (o_acc2 ),
    .I1 (d2 ),
    .O (p2 )
  ) ;
  AND2 a_g2 (
    .I0 (t2 ),
    .I1 (c1 ),
    .O (g2 )
  ) ;
  OR2 a_o2 (
    .I0 (p2 ),
    .I1 (g2 ),
    .O (c2 )
  ) ;
  XOR a_t3 (
    .I0 (o_acc3 ),
    .I1 (d3 ),
    .O (t3 )
  ) ;
  XOR a_x3 (
    .I0 (t3 ),
    .I1 (c2 ),
    .O (s3 )
  ) ;

  // -- the accumulator register --------------------------------------------
  FF acc_r0 (
    .C (clk ),
    .CE (i_en ),
    .D (s0 ),
    .Q (o_acc0 )
  ) ;
  FF acc_r1 (
    .C (clk ),
    .CE (i_en ),
    .D (s1 ),
    .Q (o_acc1 )
  ) ;
  FF acc_r2 (
    .C (clk ),
    .CE (i_en ),
    .D (s2 ),
    .Q (o_acc2 )
  ) ;
  FF acc_r3 (
    .C (clk ),
    .CE (i_en ),
    .D (s3 ),
    .Q (o_acc3 )
  ) ;

  // -- the comparator: o_acc == 4'b1010 ------------------------------------
  INV cmp_n0 (
    .I (o_acc0 ),
    .O (cn0 )
  ) ;
  INV cmp_n2 (
    .I (o_acc2 ),
    .O (cn2 )
  ) ;
  AND4 cmp_and (
    .I0 (cn0 ),
    .I1 (o_acc1 ),
    .I2 (cn2 ),
    .I3 (o_acc3 ),
    .O (o_match )
  ) ;

  // -- the controller -------------------------------------------------------
  // a' = !b & (a ? !i_fin : i_go),  b' = !b & a & i_fin
  FFR st_a (
    .C (clk ),
    .CE (const1 ),
    .D (d_a ),
    .R (const0 ),
    .Q (o_busy )
  ) ;
  FFR st_b (
    .C (clk ),
    .CE (const1 ),
    .D (d_b ),
    .R (const0 ),
    .Q (q_b )
  ) ;
  INV f_nb (
    .I (q_b ),
    .O (n_b )
  ) ;
  INV f_nf (
    .I (i_fin ),
    .O (n_fin )
  ) ;
  MUX f_mux (
    .I0 (i_go ),
    .I1 (n_fin ),
    .S (o_busy ),
    .O (m_a )
  ) ;
  AND2 f_da (
    .I0 (n_b ),
    .I1 (m_a ),
    .O (d_a )
  ) ;
  AND3 f_db (
    .I0 (n_b ),
    .I1 (o_busy ),
    .I2 (i_fin ),
    .O (d_b )
  ) ;

  // -- the parity tree nothing in the pipeline claims -----------------------
  XOR p_x0 (
    .I0 (d0 ),
    .I1 (d1 ),
    .O (px0 )
  ) ;
  XOR p_x1 (
    .I0 (d2 ),
    .I1 (d3 ),
    .O (px1 )
  ) ;
  XOR p_x2 (
    .I0 (px0 ),
    .I1 (px1 ),
    .O (o_par )
  ) ;

endmodule
