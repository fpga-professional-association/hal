// ---------------------------------------------------------------------------
// shift_debouncer - the ORIGINAL design for Agilex 3 RE walkthrough 08.
//
// A mechanical push button produces a burst of bounces for a few milliseconds
// after every press and release.  This block turns that burst into one clean,
// synchronous level plus a one-cycle "pressed" pulse.
//
// It is deliberately built out of three textbook structures, stacked:
//
//   1. a two-flop synchronizer   - btn_raw is asynchronous to clk.  Sampling it
//                                  directly would let metastability propagate
//                                  into the counter.  Two flops in series give
//                                  the first one a full clock period to settle.
//
//   2. a saturating up/down      - integrates the synchronized level.  Counts up
//      counter                     while the button reads pressed, down while it
//                                  reads released, and CLAMPS at both ends
//                                  instead of wrapping.  The clamp is what makes
//                                  it a debouncer rather than a free-running
//                                  counter: bounce noise averages out, and the
//                                  counter only reaches an end stop after
//                                  DEBOUNCE_MAX consecutive stable samples.
//
//   3. hysteresis output         - btn_state is SET when the counter saturates
//                                  high and CLEARED when it saturates low.  In
//                                  between it holds.  That is Schmitt-trigger
//                                  behaviour in the digital domain.
//
//   (+ a one-flop edge detector on btn_state for the btn_rise pulse.)
//
// Single clock domain (clk), single asynchronous active-low reset (rst_n).
// No vendor IP, no RAM, no DSP, no PLL, no explicit IO primitives - after
// Quartus synthesis this is nothing but tennm_lcell_comb + tennm_ff.
// ---------------------------------------------------------------------------

module shift_debouncer (
    input  wire clk,        // system clock, the only clock in the design
    input  wire rst_n,      // asynchronous, active-low reset
    input  wire btn_raw,    // ASYNCHRONOUS button input (bouncy, not clk-related)
    output wire btn_state,  // debounced button level, synchronous to clk
    output wire btn_rise    // one clk pulse on every clean 0->1 transition
);

    // -----------------------------------------------------------------------
    // 1. Two-flop synchronizer.
    //    sync[0] is the metastability-catching flop; sync[1] is the flop whose
    //    output is safe to use as a logic signal.  This is a 2-bit shift
    //    register: {sync[0], btn_raw} shifted in every cycle.
    // -----------------------------------------------------------------------
    reg [1:0] sync;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            sync <= 2'b00;
        else
            sync <= {sync[0], btn_raw};
    end

    wire sync_level = sync[1];

    // -----------------------------------------------------------------------
    // 2. Saturating up/down counter.
    //    4 bits, so it takes 15 consecutive stable samples to travel from one
    //    end stop to the other.  A real board would use a much wider counter
    //    (milliseconds of bounce), but the STRUCTURE is what this walkthrough
    //    is about and 4 bits keeps every combinational cone inside a single
    //    6-input ALM - see NOTES-ON-SYNTHESIS.md for why that matters here.
    // -----------------------------------------------------------------------
    localparam [3:0] CNT_MAX = 4'hF;
    localparam [3:0] CNT_MIN = 4'h0;

    reg [3:0] cnt;

    wire at_max = (cnt == CNT_MAX);   // saturation detector, high end
    wire at_min = (cnt == CNT_MIN);   // saturation detector, low end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt <= CNT_MIN;
        else if (sync_level && !at_max)
            cnt <= cnt + 4'd1;        // integrate up while pressed
        else if (!sync_level && !at_min)
            cnt <= cnt - 4'd1;        // integrate down while released
        // else: hold - THIS is the saturation
    end

    // -----------------------------------------------------------------------
    // 3. Hysteresis (Schmitt) output.
    //    Only the two end stops move the output; everything in between holds
    //    the previous decision.  A burst of bounces cannot toggle btn_state
    //    because it never lets the counter reach an end stop.
    // -----------------------------------------------------------------------
    reg state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= 1'b0;
        else if (at_max)
            state <= 1'b1;
        else if (at_min)
            state <= 1'b0;
    end

    // -----------------------------------------------------------------------
    // 4. Rising-edge detector on the debounced level.
    // -----------------------------------------------------------------------
    reg state_d;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state_d <= 1'b0;
        else
            state_d <= state;
    end

    assign btn_state = state;
    assign btn_rise  = state & ~state_d;

endmodule
