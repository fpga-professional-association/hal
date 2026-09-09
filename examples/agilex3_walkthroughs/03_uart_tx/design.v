// ---------------------------------------------------------------------------
// uart_tx -- minimal 8N1 UART transmitter (the "original design")
//
// This is the RTL that was actually synthesised for the walkthrough in
// guide.html.  It is deliberately small, single-clock and free of any vendor
// primitive (no PLL, no RAM, no DSP, no explicit IO buffers) so that the
// post-synthesis Agilex 3 netlist contains nothing but tennm_lcell_comb,
// tennm_ff and constants -- exactly the coverage that
// plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models.
//
// Frame format (8N1, LSB first).  Every slot lasts CLK_DIV clock cycles:
//
//   slot     0      1  2  3  4  5  6  7  8      9
//   line   start   d0 d1 d2 d3 d4 d5 d6 d7    stop     idle
//   value    0     <------- tx_data -------->    1       1
//
// Two counters live in the same clock domain and are the same width:
//
//   baud_cnt[3:0]  -- free-running modulo-16 prescaler; wraps on its own, so
//                     the "divisor" is not a comparison against a constant but
//                     simply the width of the counter.  CLK_DIV = 2**DIV_W.
//   bit_cnt[3:0]   -- which of the 10 frame slots is on the wire (plus a
//                     terminal value 10 that ends the frame).
//
// The width collision is on purpose.  It is the interesting case for the
// reverse-engineering exercise: a register-grouping tool cannot separate these
// two by width, only by *dataflow* -- one is clocked every cycle, the other
// only on the prescaler's terminal count.
//
// Style note: the control logic is written so that every next-state function
// has at most six inputs.  That is not cosmetic -- it keeps Quartus out of the
// ALM's fracturable 7-input ("extended LUT") mode and out of the register's
// synchronous-clear pin, both of which are outside the validated
// tools/hal_agilex primitive coverage and would make the .vo un-importable.
// ---------------------------------------------------------------------------

module uart_tx #(
    // Prescaler width.  The baud counter wraps naturally, so the number of
    // clock cycles per serial bit is exactly 2**DIV_W.
    parameter integer DIV_W = 4
) (
    input  wire       clk,       // single clock domain
    input  wire       rst_n,     // asynchronous, active low
    input  wire       tx_start,  // request a frame; sampled only while idle
    input  wire [7:0] tx_data,   // byte to send, captured at load time
    output wire       tx,        // serial output line
    output wire       tx_busy    // high while a frame is in flight
);

    localparam integer N_SLOTS = 10;  // 1 start + 8 data + 1 stop
    localparam integer BIT_W   = 4;   // enough for 0..10

    // -- state ---------------------------------------------------------------

    reg [DIV_W-1:0]   baud_cnt;  // prescaler
    reg [BIT_W-1:0]   bit_cnt;   // frame position, 0..N_SLOTS
    reg [N_SLOTS-1:0] shreg;     // the serializer
    reg               busy;      // the entire control FSM: two states

    // -- combinational -------------------------------------------------------

    // Terminal count of the prescaler: all ones, i.e. one tick every 2**DIV_W
    // clocks.  No constant comparator, just an AND of the counter bits.
    wire baud_tick = &baud_cnt;

    // The frame is over once the counter has walked past the last slot.
    wire frame_end = (bit_cnt == N_SLOTS[BIT_W-1:0]);

    assign tx      = shreg[0];   // the LSB of the shift register is the line
    assign tx_busy = busy;

    // -- sequential ----------------------------------------------------------

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            baud_cnt <= {DIV_W{1'b0}};
            bit_cnt  <= {BIT_W{1'b0}};
            shreg    <= {N_SLOTS{1'b1}};   // idle line is high
            busy     <= 1'b0;
        end
        else if (!busy) begin
            // Idle: both counters parked at zero, waiting for a request.
            baud_cnt <= {DIV_W{1'b0}};
            bit_cnt  <= {BIT_W{1'b0}};
            if (tx_start) begin
                // Parallel load: stop bit, data LSB-first, start bit.
                // shreg[0] (= tx) becomes the start bit on the very next edge.
                shreg <= {1'b1, tx_data, 1'b0};
                busy  <= 1'b1;
            end
        end
        else begin
            // Busy: prescaler runs every cycle and wraps by itself.
            baud_cnt <= baud_cnt + 1'b1;

            if (baud_tick) begin
                // One serial bit has elapsed: advance the frame.
                shreg   <= {1'b1, shreg[N_SLOTS-1:1]};  // shift right, fill 1
                bit_cnt <= bit_cnt + 1'b1;
            end

            // bit_cnt reaches N_SLOTS one prescaler period after the stop bit
            // started, i.e. when the stop bit has been held for a full slot.
            // tx_busy therefore falls one clock after the frame is complete.
            if (frame_end) busy <= 1'b0;
        end
    end

endmodule
