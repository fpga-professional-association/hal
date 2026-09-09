// ---------------------------------------------------------------------------
// traffic_fsm -- a 4-state Moore traffic-light controller.
//
// This is the *original* RTL for the walkthrough in guide.html.  It is written
// to be small enough to reverse-engineer by hand, while still containing the
// two structures a real controller has: a state register with genuine feedback,
// and a dwell-time counter that paces it.
//
// Deliberate restrictions (so the synthesized netlist stays inside the
// primitive coverage of plugins/gate_libraries/definitions/AGILEX_TENNM.hgl):
//   * one clock, one asynchronous active-low reset, no other clocking;
//   * no vendor IP, no RAM/DSP/PLL/IO primitives, no initial blocks;
//   * no synchronous clear/load idioms -- the .qsf turns synchronous control
//     ports off (ALLOW_SYNCH_CTRL_USAGE OFF) so every register maps onto the
//     tennm_ff configuration hal_agilex models (D, ENA, CLRN only).
//
// Behaviour: the light cycles R -> R+Y -> G -> Y -> R forever.  Each phase
// lasts a fixed number of clock cycles; `hold` freezes the whole controller
// (both the counter and the state register) in place.
// ---------------------------------------------------------------------------

module traffic_fsm (
    input  wire clk,     // single clock
    input  wire rst_n,   // asynchronous, active low; drops the light to RED
    input  wire hold,    // 1 = freeze the controller in the current phase
    output wire red,     // lamp outputs -- Moore, decoded from `state` only
    output wire yellow,
    output wire green
);

    // -----------------------------------------------------------------------
    // State encoding.  Chosen so the lamp decode is nearly free:
    //   red    = !state[1]           (RED, RED_YELLOW)
    //   yellow =  state[0]           (RED_YELLOW, YELLOW)
    //   green  =  state[1] & !state[0]
    // Quartus is free to re-encode this; recovering *which* encoding survived
    // is part of the exercise in guide.html.
    // -----------------------------------------------------------------------
    localparam [1:0] S_RED        = 2'b00,
                     S_RED_YELLOW = 2'b01,
                     S_GREEN      = 2'b10,
                     S_YELLOW     = 2'b11;

    // Dwell time of each phase, in clock cycles, minus one (the counter runs
    // 0 .. DWELL-1 and the phase ends on the last value).
    localparam [3:0] T_RED        = 4'd9,
                     T_RED_YELLOW = 4'd2,
                     T_GREEN      = 4'd12,
                     T_YELLOW     = 4'd4;

    reg  [1:0] state;
    reg  [3:0] tick;

    // Dwell limit for the phase we are currently in.
    reg  [3:0] limit;
    always @* begin
        case (state)
            S_RED        : limit = T_RED;
            S_RED_YELLOW : limit = T_RED_YELLOW;
            S_GREEN      : limit = T_GREEN;
            default      : limit = T_YELLOW;
        endcase
    end

    // The phase is over when the counter has reached the limit for this phase.
    wire expired = (tick == limit);
    wire advance = expired & ~hold;

    // Next dwell-counter value: restart at 0 on a phase change, hold while
    // `hold` is asserted, otherwise count up.  Written as an explicit mux so
    // the synthesizer builds it out of LUT logic in front of D rather than out
    // of a synchronous-clear port.
    reg [3:0] tick_next;
    always @* begin
        if (hold)         tick_next = tick;
        else if (expired) tick_next = 4'd0;
        else              tick_next = tick + 4'd1;
    end

    // Next state: the fixed cycle R -> R+Y -> G -> Y -> R, taken only when the
    // current phase has expired and we are not held.
    reg [1:0] state_next;
    always @* begin
        state_next = state;
        if (advance) begin
            case (state)
                S_RED        : state_next = S_RED_YELLOW;
                S_RED_YELLOW : state_next = S_GREEN;
                S_GREEN      : state_next = S_YELLOW;
                default      : state_next = S_RED;
            endcase
        end
    end

    // The only sequential elements in the design.  Asynchronous reset only.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_RED;
            tick  <= 4'd0;
        end else begin
            state <= state_next;
            tick  <= tick_next;
        end
    end

    // Moore outputs: a pure function of `state`.
    assign red    = (state == S_RED) | (state == S_RED_YELLOW);
    assign yellow = (state == S_RED_YELLOW) | (state == S_YELLOW);
    assign green  = (state == S_GREEN);

endmodule
