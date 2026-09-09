// -----------------------------------------------------------------------------
// recovered.v - the design as reconstructed from netlist/netlist.hal.v alone.
//
// Written from the structural evidence collected in guide.html:
//
//   * 16 tennm_ff in two enable groups of 8            -> two 8-bit registers
//   * a 7-cell carry chain whose sumout feeds group A
//     and whose data inputs are group A's own q        -> A is an incrementer
//   * cell [0] of that chain has cin = gnd and its
//     bit-0 partner is a separate inverter LUT         -> the increment is +1
//   * a 6-cell carry chain reading A and B two bits at
//     a time, only its last cout leaving the chain     -> magnitude comparator
//   * the comparator's polarity, from the masks        -> A < B  (unsigned)
//   * two AND-reduce LUTs over all 8 bits of A         -> A == 8'hFF
//   * group B's enable is a 3-input LUT of cfg_we and
//     cfg_addr                                         -> address-decoded write
//
// Deliberately neutral names: the exported netlist happens to have kept the
// original RTL identifiers, and none of the reasoning above used them.
//
//   reg_a      <-> the counter          (netlist: cnt[7:0])
//   reg_b      <-> the threshold        (netlist: duty[7:0])
//   reg_b_load <-> the decoded write    (netlist: duty_we~0_combout)
//
// Verified against the export: `check.py` simulates this model against the
// netlist for 200 pseudo-random cycles and, separately, over all 65536
// (reg_a, reg_b) pairs.
// -----------------------------------------------------------------------------

module recovered_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       run,
    input  wire       cfg_we,
    input  wire [1:0] cfg_addr,
    input  wire [7:0] cfg_wdata,
    output wire       pwm_out,
    output wire       period_tick
);

    // Register group A: 8 tennm_ff, clk = clk, ena = run, clrn = rst_n.
    reg [7:0] reg_a;

    // Register group B: 8 tennm_ff, clk = clk, clrn = rst_n,
    // ena = the decode LUT below.
    reg [7:0] reg_b;

    // The decode LUT, read straight off its lut_mask:
    //   duty_we~0_combout = cfg_we & cfg_addr[0] & !cfg_addr[1]
    wire reg_b_load = cfg_we & cfg_addr[0] & ~cfg_addr[1];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)     reg_a <= 8'h00;
        else if (run)   reg_a <= reg_a + 8'd1;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)          reg_b <= 8'h00;
        else if (reg_b_load) reg_b <= cfg_wdata;
    end

    // Comparator chain: cout of the last cell, buffered through
    // LessThan_0~1 (sumout = !cin) and LessThan_0~1_wirecell (an inverter),
    // i.e. two inversions that cancel.
    assign pwm_out = (reg_a < reg_b);

    // reduce_nor_1~0 = &reg_a[3:0], reduce_nor_1 = that & &reg_a[7:4].
    assign period_tick = (reg_a == 8'hFF);

endmodule
