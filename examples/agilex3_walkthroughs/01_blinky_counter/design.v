// ---------------------------------------------------------------------------
// blinky_counter -- the "original design" of Agilex 3 walkthrough 01.
//
// A 24-bit free-running counter whose most significant bit drives an LED.
// This is the smallest circuit that still contains everything a netlist
// reverse engineer has to recognise in real hardware:
//
//   * a register bank that is driven *by a function of itself*  -> a feedback
//     loop, which is what makes it state and not just combinational logic;
//   * an increment implemented on the FPGA's dedicated carry chain, so the
//     "+ 1" is not a visible adder but a chain of ALM cells wired cout -> cin;
//   * a single output tap that hides the width of the counter behind one pin.
//
// Deliberately *not* used, so that the post-synthesis netlist contains only
// primitives that plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models
// (tennm_lcell_comb, tennm_ff and the two constant cells the importer adds):
// no PLL, no I/O buffers (we stop after synthesis, before fitting), no RAM,
// no DSP, no clock enable logic and no second clock domain.
// ---------------------------------------------------------------------------

`default_nettype none

module blinky_counter (
    // The only clock in the design.  Everything below is synchronous to its
    // rising edge, which is what makes the recovered design a single-domain
    // circuit and lets us skip clock-domain-crossing analysis entirely.
    input  wire clk,

    // Active-low asynchronous reset.  Quartus maps this onto the ALM
    // register's dedicated `clrn` pin, so in the netlist it shows up as a net
    // that reaches *every* flip-flop's clrn and nothing else -- one of the
    // strongest structural fingerprints there is.
    input  wire rst_n,

    // The blink output.  It is bit 23 of the counter, so it toggles once
    // every 2**23 clock cycles: at 50 MHz that is a ~3 Hz blink
    // (50e6 / 2**24 = 2.98 Hz full cycles per second).
    output wire led
);

    // The counter state.  24 bits, i.e. 24 flip-flops in the netlist.
    reg [23:0] count;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // Asynchronous clear -> tennm_ff.clrn
            count <= 24'd0;
        end else begin
            // Free-running increment.  No enable: every flip-flop's `ena` pin
            // ends up tied to the constant 1 in the netlist, which is exactly
            // why a naive "find the enable signal" search finds nothing here.
            count <= count + 24'd1;
        end
    end

    // The single observable tap.  Nothing else leaves the module, so from the
    // outside this design is indistinguishable from a much simpler one -- the
    // width of the counter can only be recovered from the *internal*
    // structure, never from the port list.
    assign led = count[23];

endmodule

`default_nettype wire
