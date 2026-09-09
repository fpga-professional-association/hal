// -----------------------------------------------------------------------------
// pwm_generator - 8-bit PWM with a programmable duty threshold register.
//
// This is the ORIGINAL design of walkthrough 04. It is the thing a reverse
// engineer is trying to recover from the synthesized netlist; do not read it
// while you are working through the guide unless you want the answer.
//
// Structure (three pieces, deliberately):
//
//   1. a free-running 8-bit counter `cnt`, enabled by `run`
//   2. a magnitude comparator `cnt < duty` driving `pwm_out`
//   3. an 8-bit configuration register `duty`, written through a tiny
//      address-decoded enable interface (cfg_we & cfg_addr == 2'b01)
//
// Every construct here maps onto primitives the AGILEX_TENNM gate library
// models: LUTs (tennm_lcell_comb, normal and arithmetic mode), flip-flops
// (tennm_ff), and the constant gates. No RAM, no DSP, no PLL, no IO buffers.
// -----------------------------------------------------------------------------

module pwm_generator (
    input  wire       clk,          // single clock domain
    input  wire       rst_n,        // asynchronous, active low
    input  wire       run,          // counter enable: freezes the ramp when low

    // Configuration write port. One writable register lives at address 1;
    // the decode is intentionally *not* a bare port so that the write path
    // has to be recovered from logic rather than read off a pin name.
    input  wire       cfg_we,
    input  wire [1:0] cfg_addr,
    input  wire [7:0] cfg_wdata,

    output wire       pwm_out,      // 1 while cnt < duty
    output wire       period_tick   // 1 on the last count of each period
);

    localparam [1:0] ADDR_DUTY = 2'b01;

    // ---------------------------------------------------------------- counter
    reg [7:0] cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      cnt <= 8'h00;
        else if (run)    cnt <= cnt + 8'd1;   // wraps 255 -> 0, defines the period
    end

    // ------------------------------------------------- duty threshold register
    wire duty_we = cfg_we & (cfg_addr == ADDR_DUTY);

    reg [7:0] duty;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)      duty <= 8'h00;       // reset duty = 0 -> output parked low
        else if (duty_we) duty <= cfg_wdata;
    end

    // ------------------------------------------------------------- comparator
    // Unsigned magnitude compare. duty = 0 gives a permanently low output,
    // duty = 255 gives 255/256 duty cycle. There is no duty = 100% code.
    assign pwm_out = (cnt < duty);

    // ------------------------------------------------------------ period flag
    assign period_tick = (cnt == 8'hFF);

endmodule
