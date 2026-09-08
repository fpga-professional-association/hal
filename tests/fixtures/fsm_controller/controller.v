// A hand-written fixture for tools/hal_fsm. See README.md in this directory for
// the design intent and ground_truth.json for the reference transition tables.
//
// Gate library: plugins/gate_libraries/definitions/example_library.hgl
//
// Three registers share this netlist on purpose:
//
//   nx_a1_reg / nx_b2_reg   a 3-state controller whose state bits carry no
//                           meaningful names, so a tool cannot recognise them
//                           from the netlist text
//   cnt_r0..cnt_r2          a 3-bit counter -- also a state machine, also with
//                           feedback, and the decoy a candidate search has to
//                           rank below the controller
//   dp_x0 / dp_x1           a two-stage pipeline register with no feedback at
//                           all, which must not be proposed as a state register
//
// The controller's flip-flops have their asynchronous reset tied to ground; the
// counter's is driven by i_rst. solve_fsm models neither, so the counter is also
// the fixture for "an asynchronous reset that is not part of the model".

module ctrl_top (
  clk,
  i_go,
  i_fin,
  i_tick,
  i_rst,
  o_busy,
  o_done,
  o_cnt0,
  o_cap
 ) ;
  input clk ;
  input i_go ;
  input i_fin ;
  input i_tick ;
  input i_rst ;
  output o_busy ;
  output o_done ;
  output o_cnt0 ;
  output o_cap ;
  wire const0 ;
  wire const1 ;
  wire q_a ;
  wire q_b ;
  wire d_a ;
  wire d_b ;
  wire n_b ;
  wire n_fin ;
  wire m_a ;
  wire q_c0 ;
  wire q_c1 ;
  wire q_c2 ;
  wire d_c0 ;
  wire d_c1 ;
  wire d_c2 ;
  wire c1en ;
  wire c2en ;
  wire q_d0 ;
  wire q_d1 ;

GND u_gnd (
  .\O (const0 )
 ) ;
VCC u_vcc (
  .\O (const1 )
 ) ;

// -- the controller ---------------------------------------------------------
// next state:  a' = !b & (a ? !i_fin : i_go)
//              b' = !b &  a &  i_fin
FFR #(.INIT(1'h0))
nx_a1_reg (
  .\C (clk ),
  .\CE (const1 ),
  .\D (d_a ),
  .\R (const0 ),
  .\Q (q_a )
 ) ;
FFR #(.INIT(1'h0))
nx_b2_reg (
  .\C (clk ),
  .\CE (const1 ),
  .\D (d_b ),
  .\R (const0 ),
  .\Q (q_b )
 ) ;
INV u_inv_b (
  .\I (q_b ),
  .\O (n_b )
 ) ;
INV u_inv_f (
  .\I (i_fin ),
  .\O (n_fin )
 ) ;
MUX u_mux_a (
  .\I0 (i_go ),
  .\I1 (n_fin ),
  .\S (q_a ),
  .\O (m_a )
 ) ;
AND2 u_and_a (
  .\I0 (n_b ),
  .\I1 (m_a ),
  .\O (d_a )
 ) ;
AND3 u_and_b (
  .\I0 (n_b ),
  .\I1 (q_a ),
  .\I2 (i_fin ),
  .\O (d_b )
 ) ;
BUF u_busy_buf (
  .\I (q_a ),
  .\O (o_busy )
 ) ;
BUF u_done_buf (
  .\I (q_b ),
  .\O (o_done )
 ) ;

// -- the decoy counter ------------------------------------------------------
// c0' = c0 ^ i_tick, c1' = c1 ^ (c0 & i_tick), c2' = c2 ^ (c0 & c1 & i_tick)
FFR #(.INIT(1'h0))
cnt_r0 (
  .\C (clk ),
  .\CE (const1 ),
  .\D (d_c0 ),
  .\R (i_rst ),
  .\Q (q_c0 )
 ) ;
FFR #(.INIT(1'h0))
cnt_r1 (
  .\C (clk ),
  .\CE (const1 ),
  .\D (d_c1 ),
  .\R (i_rst ),
  .\Q (q_c1 )
 ) ;
FFR #(.INIT(1'h0))
cnt_r2 (
  .\C (clk ),
  .\CE (const1 ),
  .\D (d_c2 ),
  .\R (i_rst ),
  .\Q (q_c2 )
 ) ;
XOR u_x0 (
  .\I0 (q_c0 ),
  .\I1 (i_tick ),
  .\O (d_c0 )
 ) ;
AND2 u_c1en (
  .\I0 (q_c0 ),
  .\I1 (i_tick ),
  .\O (c1en )
 ) ;
XOR u_x1 (
  .\I0 (q_c1 ),
  .\I1 (c1en ),
  .\O (d_c1 )
 ) ;
AND3 u_c2en (
  .\I0 (q_c0 ),
  .\I1 (q_c1 ),
  .\I2 (i_tick ),
  .\O (c2en )
 ) ;
XOR u_x2 (
  .\I0 (q_c2 ),
  .\I1 (c2en ),
  .\O (d_c2 )
 ) ;
BUF u_cnt0_buf (
  .\I (q_c0 ),
  .\O (o_cnt0 )
 ) ;

// -- the pipeline register (no feedback, must not be a candidate) ------------
FF #(.INIT(1'h0))
dp_x0 (
  .\C (clk ),
  .\CE (const1 ),
  .\D (i_go ),
  .\Q (q_d0 )
 ) ;
FF #(.INIT(1'h0))
dp_x1 (
  .\C (clk ),
  .\CE (const1 ),
  .\D (q_d0 ),
  .\Q (q_d1 )
 ) ;
BUF u_cap_buf (
  .\I (q_d1 ),
  .\O (o_cap )
 ) ;

endmodule
