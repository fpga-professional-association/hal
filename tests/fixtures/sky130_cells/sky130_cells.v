// sky130_cells.v -- an original, hand-written structural netlist over the shipped
// plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl gate library.
//
// It is a fixture, not a design: it exists so that runTest-gate_library_sky130 can
// check that the library parses and that the cell shapes an extracted sky130 netlist
// relies on survive a round trip through the Verilog parser. See README.md for the
// documented expected structure -- keep the two in sync.
//
// Written in the shape the sky130 flows emit: escaped identifiers, named port
// connections, no power pins.

module sky130_cells (
    clk,
    rst_n,
    set_n,
    din,
    dout
);
  input clk;
  input rst_n;
  input set_n;
  input din;
  output [7:0] dout;

  wire tie_hi;
  wire tie_lo;
  wire n_nandb;
  wire n_inv;
  wire n_en;
  wire n_and;
  wire n_aoi;
  wire n_oai;

  // constants: HI is tied high, LO is tied low
  sky130_fd_sc_hd__conb_1 \tie_cell  (
      .HI(tie_hi),
      .LO(tie_lo)
  );

  // inverted-pin naming: A_N is the inverted input of the NAND
  sky130_fd_sc_hd__nand2b_1 \u_din_nand  (
      .A_N(rst_n),
      .B(din),
      .Y(n_nandb)
  );

  sky130_fd_sc_hd__inv_1 \u_inv  (
      .A(n_nandb),
      .Y(n_inv)
  );

  // second nand2b, fed by the constant cell: Y = !(!1 & 1) = 1
  sky130_fd_sc_hd__nand2b_1 \u_tie_nand  (
      .A_N(tie_hi),
      .B(tie_hi),
      .Y(n_en)
  );

  sky130_fd_sc_hd__and2_1 \u_and  (
      .A(n_inv),
      .B(n_en),
      .X(n_and)
  );

  // four-bit shift register: two async-reset-low, one async-set-low, one plain
  sky130_fd_sc_hd__dfrtp_2 \state_reg[0]  (
      .CLK(clk),
      .D(n_and),
      .RESET_B(rst_n),
      .Q(dout[0])
  );

  sky130_fd_sc_hd__dfrtp_2 \state_reg[1]  (
      .CLK(clk),
      .D(dout[0]),
      .RESET_B(rst_n),
      .Q(dout[1])
  );

  sky130_fd_sc_hd__dfstp_2 \state_reg[2]  (
      .CLK(clk),
      .D(dout[1]),
      .SET_B(set_n),
      .Q(dout[2])
  );

  sky130_fd_sc_hd__dfxtp_2 \state_reg[3]  (
      .CLK(clk),
      .D(dout[2]),
      .Q(dout[3])
  );

  sky130_fd_sc_hd__xor2_1 \u_xor  (
      .A(dout[0]),
      .B(dout[1]),
      .X(dout[4])
  );

  // the second constant leg: Y = !(q3 ^ 0) = !q3
  sky130_fd_sc_hd__xnor2_1 \u_xnor  (
      .A(dout[3]),
      .B(tie_lo),
      .Y(dout[5])
  );

  sky130_fd_sc_hd__a21oi_1 \u_aoi  (
      .A1(dout[0]),
      .A2(dout[1]),
      .B1(dout[5]),
      .Y(n_aoi)
  );

  sky130_fd_sc_hd__o21ai_1 \u_oai  (
      .A1(dout[2]),
      .A2(dout[3]),
      .B1(dout[4]),
      .Y(n_oai)
  );

  sky130_fd_sc_hd__buf_1 \buf_aoi_x  (
      .A(n_aoi),
      .X(dout[6])
  );

  sky130_fd_sc_hd__buf_1 \buf_oai_x  (
      .A(n_oai),
      .X(dout[7])
  );

endmodule
