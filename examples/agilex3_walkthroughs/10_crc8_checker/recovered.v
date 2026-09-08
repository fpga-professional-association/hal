// ---------------------------------------------------------------------------
// recovered_crc8 -- RTL reconstructed from netlist/netlist.anon.hal.v ALONE.
//
// Nothing in this file was copied from design.v.  Every line below is backed by
// something a tool printed during the walk in guide.html; the comment on each
// line says which observation it came from.  The names are ours: the netlist
// had `u0..u12`, `n0..n12`, `port_i0..3`, `port_o0..1` and nothing else.
//
// What could NOT be recovered, and is therefore invented here:
//   * every identifier (module, ports, register, the local parameter);
//   * the fact that `en` is called an enable rather than a "valid" or a
//     "shift" strobe -- the netlist only shows it gates the flip-flops;
//   * hierarchy -- there was none left to recover, the export is flat;
//   * the coding style: design.v wrote the eight next-state equations out one
//     by one, this file writes the equivalent shift-and-conditional-xor.
//     Both synthesise to the same 13 cells; the netlist cannot tell them apart.
// ---------------------------------------------------------------------------

module recovered_crc8 (
    input  wire       clk,        // port_i1: the only net reaching all 8 tennm_ff .clk pins
    input  wire       rst_n,      // port_i2: the only net reaching all 8 .clrn pins;
                                  //          tennm_ff models clrn as ASYNCHRONOUS and
                                  //          ACTIVE LOW, so this is an async reset to 0
    input  wire       en,         // port_i3: the only net reaching all 8 .ena pins
    input  wire       din,        // port_i0: reaches only ALM data inputs, never a
                                  //          control pin -- the serial data bit
    output wire [7:0] state,      // port_o0: driven straight by the 8 register outputs;
                                  //          bus index == position in the recovered chain
    output wire       zero_flag   // port_o1: truth table over `state` is exactly state==0
);

    // Recovered from the tap set: the three next-state functions that contain
    // the data bit are the ones at chain positions 0, 1 and 2.  For a CRC in
    // this (Galois, MSB-first) form the tapped positions ARE the set bits of
    // the generator polynomial's low byte.
    //     0b0000_0111 = 8'h07  ->  x^8 + x^2 + x + 1
    localparam [7:0] POLY = 8'h07;

    reg [7:0] q;

    // Recovered from the single strongly connected component: chain position 7
    // is the only register that feeds back, and it is xored with `din` in every
    // tapped stage.  ALM u10 (lut_mask 64'h6666666666666666) is exactly this
    // 2-input XOR; u12 and u1 (64'h9696969696969696) are the 3-input XOR that
    // folds it into stages 1 and 2.
    wire feedback = q[7] ^ din;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // Recovered from the primitive, not from the design: `clrn` low
            // forces q to 0 in the tennm_ff model, and no stage was inverted
            // (every next-state function had constant term 0), so the reset
            // value is 8'h00.
            q <= 8'h00;
        end else if (en) begin
            q <= {q[6:0], 1'b0} ^ (feedback ? POLY : 8'h00);
        end
    end

    assign state = q;

    // Recovered by enumerating the truth table of port_o1 over the 256 states:
    // it is 1 for state 0 and 0 for all others.  Structurally it is two ALMs,
    // a 4-input NOR (u6, 64'h0001000100010001) feeding a 5-input AND-of-NOR
    // (u8, 64'h0001000000010000) -- an 8-input NOR split across two cells.
    assign zero_flag = ~|q;

endmodule
