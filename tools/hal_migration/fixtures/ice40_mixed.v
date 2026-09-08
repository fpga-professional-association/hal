// Migration-assessment fixture: a small hand-written iCE40 UltraPlus netlist.
//
// It is not a working design. Every instance exists to put one *kind* of
// migration question into the inventory:
//
//   SB_LUT4      combinational logic that maps cleanly
//   SB_DFF       a plain register (supported by the bundled catalogue)
//   SB_DFFE      a register with clock enable (supported)
//   SB_DFFR      a register the gate library models with an asynchronous clear
//   SB_DFFSR     a register whose reset is folded into the next-state function
//   SB_DFFNSR    a NEGATIVE-EDGE register, deliberately absent from the
//                catalogue -> must be reported as unresolved, not ignored
//   SB_CARRY     dedicated carry logic
//   SB_RAM40_4K  a block RAM whose bit size and port structure the gate library
//                does not model -> the mapping must be downgraded to unresolved
//   SB_MAC16     a hard DSP block
//   SB_IO        an I/O primitive (constraints live outside the netlist)
//   SB_GB        a global clock buffer
//   SB_HFOSC     an on-chip oscillator (no generic target equivalent)
//   SB_I2C       hard IP (no generic target equivalent)
//   GND, VCC     constant drivers
//
// Parse it with the ICE40ULTRA gate library:
//   plugins/gate_libraries/definitions/ice40ultra.hgl
//
// The expected inventory is stated in fixtures/README.md and asserted by
// test_hal_migration_hal.py.

module ice40_mixed (
    clk_pin,
    rst,
    d_in,
    ce,
    i2c_scl,
    ram_addr,
    ram_wdata,
    pad,
    q_out,
    i2c_sda_out
);

    input clk_pin;
    input rst;
    input d_in;
    input ce;
    input i2c_scl;
    input [10:0] ram_addr;
    input [15:0] ram_wdata;
    inout pad;
    output q_out;
    output i2c_sda_out;

    wire osc_clk;
    wire clk_g;
    wire gnd_net;
    wire vcc_net;
    wire q1;
    wire q2;
    wire q3;
    wire q4;
    wire q5;
    wire q6;
    wire q7;
    wire lut_o1;
    wire lut_o2;
    wire carry_co;
    wire pad_in;
    wire [15:0] ram_rdata;
    wire [31:0] mac_o;

    // ---- constants --------------------------------------------------------
    GND gnd_inst (
        .Y(gnd_net)
    );

    VCC vcc_inst (
        .Y(vcc_net)
    );

    // ---- clocking ---------------------------------------------------------
    // The oscillator output is a clock source; the global buffer distributes
    // the externally supplied clock. Note that HAL cannot tell that clk_pin is
    // a clock: the input pin of SB_GB has pin type 'none'.
    SB_HFOSC osc_inst (
        .CLKHFEN(vcc_net),
        .CLKHFPU(vcc_net),
        .CLKHF(osc_clk)
    );

    SB_GB clk_buffer (
        .USER_SIGNAL_TO_GLOBAL_BUFFER(clk_pin),
        .GLOBAL_BUFFER_OUTPUT(clk_g)
    );

    // ---- combinational ----------------------------------------------------
    SB_LUT4 lut_a (
        .I0(d_in),
        .I1(pad_in),
        .I2(q2),
        .I3(gnd_net),
        .O(lut_o1)
    );

    SB_LUT4 lut_b (
        .I0(lut_o1),
        .I1(q4),
        .I2(q6),
        .I3(carry_co),
        .O(lut_o2)
    );

    SB_CARRY carry_a (
        .CI(gnd_net),
        .I0(lut_o1),
        .I1(q1),
        .CO(carry_co)
    );

    // ---- registers --------------------------------------------------------
    SB_DFF ff_a (
        .C(clk_g),
        .D(lut_o1),
        .Q(q1)
    );

    SB_DFF ff_b (
        .C(clk_g),
        .D(lut_o2),
        .Q(q2)
    );

    SB_DFFE ff_enable (
        .C(clk_g),
        .E(ce),
        .D(q1),
        .Q(q3)
    );

    SB_DFFR ff_async_reset_a (
        .C(clk_g),
        .R(rst),
        .D(q3),
        .Q(q4)
    );

    SB_DFFR ff_async_reset_b (
        .C(clk_g),
        .R(rst),
        .D(q4),
        .Q(q5)
    );

    SB_DFFSR ff_sync_reset (
        .C(clk_g),
        .R(rst),
        .D(q5),
        .Q(q6)
    );

    // Negative-edge flip-flop on the oscillator clock: this gate type is not in
    // the bundled catalogue, so the assessment must report it as unresolved.
    SB_DFFNSR ff_negedge (
        .C(osc_clk),
        .R(rst),
        .D(q6),
        .Q(q7)
    );

    // ---- memory -----------------------------------------------------------
    SB_RAM40_4K ram_inst (
        .WE(ce),
        .WCLK(clk_g),
        .WCLKE(vcc_net),
        .RE(vcc_net),
        .RCLK(clk_g),
        .RCLKE(vcc_net),
        .WADDR(ram_addr),
        .RADDR(ram_addr),
        .WDATA(ram_wdata),
        .RDATA(ram_rdata)
    );

    // ---- arithmetic -------------------------------------------------------
    SB_MAC16 mac_inst (
        .CLK(clk_g),
        .CE(ce),
        .A(ram_wdata),
        .B(ram_rdata),
        .O(mac_o)
    );

    // ---- I/O and hard IP --------------------------------------------------
    SB_IO io_inst (
        .PACKAGE_PIN(pad),
        .OUTPUT_ENABLE(vcc_net),
        .D_OUT_0(q7),
        .D_IN_0(pad_in)
    );

    SB_I2C i2c_inst (
        .SBCLKI(clk_g),
        .SBRWI(gnd_net),
        .SBSTBI(gnd_net),
        .SCLI(i2c_scl),
        .SDAO(i2c_sda_out)
    );

    // ---- top-level output -------------------------------------------------
    SB_DFF ff_out (
        .C(clk_g),
        .D(q6),
        .Q(q_out)
    );

endmodule
