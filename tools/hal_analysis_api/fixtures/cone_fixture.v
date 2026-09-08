// A hand-crafted netlist for the scoped-query tests of tools/hal_analysis_api.
//
// It is written against EXAMPLE_GATE_LIBRARY
// (plugins/gate_libraries/definitions/example_library.hgl, the same library
// examples/uart.zip ships), and its instantiation style mirrors uart.v so the
// HAL Verilog parser sees nothing unusual.
//
// Seven gates, deliberately shaped so that fan-in and fan-out cones have
// different, hand-checkable answers and there is a feedback loop through the
// two flip-flops:
//
//     CLK --> clk_buf --+--> ff0.C
//                       +--> ff1.C
//     EN  ------------- +--> ff0.CE, ff1.CE
//     EN  --> en_inv --> and0.I1
//     ff0.Q ----------> and0.I0
//     and0.O ---------> ff1.D
//     ff1.Q ----------+-> inv0.I --> ff0.D          (the loop)
//                     +-> out_buf.I --> OUT
//
// Every expected answer is recorded in cone_fixture.ground_truth.json; see
// fixtures/README.md for what is asserted where.

module cone_fixture (
  CLK,
  EN,
  OUT
 ) ;
  input CLK ;
  input EN ;
  output OUT ;
  wire clk_i ;
  wire en_n ;
  wire q0 ;
  wire q1 ;
  wire d0 ;
  wire d1 ;

BUF
clk_buf (
  .\I (CLK ),
  .\O (clk_i )
 ) ;
INV
en_inv (
  .\I (EN ),
  .\O (en_n )
 ) ;
FF #(.INIT(1'h0))
ff0 (
  .\C (clk_i ),
  .\CE (EN ),
  .\D (d0 ),
  .\Q (q0 )
 ) ;
FF #(.INIT(1'h0))
ff1 (
  .\C (clk_i ),
  .\CE (EN ),
  .\D (d1 ),
  .\Q (q1 )
 ) ;
AND2
and0 (
  .\I0 (q0 ),
  .\I1 (en_n ),
  .\O (d1 )
 ) ;
INV
inv0 (
  .\I (q1 ),
  .\O (d0 )
 ) ;
BUF
out_buf (
  .\I (q1 ),
  .\O (OUT )
 ) ;
endmodule
