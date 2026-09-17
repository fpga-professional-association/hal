// ---------------------------------------------------------------------------
// core_b -- GROUND TRUTH for walkthrough 16.  Do not read this while doing the
// exercise; cores/core_b.anon.hal.v is the exercise.
//
// The decoy.  This is the only design in the set that was written for this
// walkthrough, and the only one that is not cryptography: a byte-oriented
// framing receiver for a self-synchronising serial link.  It exists so that
// `none-detected` is exercised as an *honest* outcome and not merely as the
// thing that happens when a pass fails.
//
// It is deliberately built out of the parts a cipher is also built out of:
//
//   * an eight-stage shift register clocked every cycle, which is the same
//     object walkthrough 05's LFSR and walkthrough 13's Trivium are made of --
//     except that its input is an external pin, so it absorbs data instead of
//     feeding itself, and it therefore has no feedback function at all;
//   * a sixteen-bit watchdog counter on a real sixteen-cell carry chain, which
//     is *exactly as wide as each of the two chains in core_d*, so that "there
//     is a sixteen-bit carry chain in here" cannot be read as "there is an
//     adder in a round function";
//   * two eight-bit equality comparisons against a constant, which is the
//     closest thing in the design to a table lookup and is nothing of the kind.
//
// What it does *not* contain: any feedback of state into itself beyond the
// counters, any nonlinear next-state function of more than the control bits,
// any substitution, any permutation, and -- pointedly -- no scrambler and no
// CRC.  A framing CRC would have made it an LFSR with an input, which is
// walkthrough 10, which is a structure `hal_crypto` recognises and reports.
// The decoy is only a decoy if the honest answer really is "nothing here".
//
// The protocol
// ------------
// Bits arrive one per clock on `rx`, most significant bit of a byte first.  A
// frame is
//
//     0x7E  <len>  <len payload bytes>  0x7E
//
// and the receiver is self-synchronising: it hunts for the start delimiter one
// bit at a time, then counts whole bytes.  `rx_valid` pulses for one cycle per
// payload byte, `frame_ok` for one cycle per correctly closed frame and
// `frame_err` for one cycle on a zero length, a missing end delimiter, or a
// frame that outstays the watchdog.  There is no byte stuffing, so a payload
// byte equal to 0x7E is legal and simply does not resynchronise anything -- the
// length field, not the delimiter, decides where the payload ends.
//
// The watchdog is sixteen bits and a frame is at most 255 payload bytes, so a
// frame cannot last 65536 cycles and the watchdog cannot fire while bits keep
// arriving.  It is live logic guarding an unreachable condition, which is what
// watchdogs usually are, and the reveal uses it as the negative control that a
// bounded behaviour check *cannot* catch.
//
// Staying inside the validated primitive coverage
// -----------------------------------------------
// Same two constraints as every other walkthrough in the series: no primitive
// outside `tennm_lcell_comb` / `tennm_ff` (so no memory, no DSP, and
// ALLOW_SYNCH_CTRL_USAGE OFF in the QSF keeps the load selection out of the
// flip-flop's `sload`/`sclr` pins), and no register whose next-state cone reads
// more than the six inputs of an ALM in normal mode -- a seventh sends Quartus
// to the fracturable seven/eight-input mode, which `tools/hal_agilex` reports
// as `unsupported`.  The `keep` attributes below are what holds the control
// terms out as their own cells instead of letting them merge into a wider cone;
// walkthrough 14 needed the same attributes for the same reason.
// ---------------------------------------------------------------------------

`default_nettype none

module core_b (
    // The only clock in the design.
    input  wire       clk,

    // Active-low asynchronous reset, on every flip-flop's dedicated clrn pin.
    input  wire       rst_n,

    // The serial input: one bit per clock, msb of a byte first.
    input  wire       rx,

    // The most recently completed payload byte, valid while rx_valid is high.
    output wire [7:0] rx_data,
    output wire       rx_valid,

    // One cycle per frame that closed correctly / did not.
    output wire       frame_ok,
    output wire       frame_err,

    // The one-hot receiver state, brought out so the link can be debugged.
    output wire [3:0] link_state
);

    // ---- the frame delimiter ----------------------------------------------
    localparam [7:0] DELIMITER = 8'h7E;

    // ---- state -------------------------------------------------------------
    reg [7:0] sh;            // the receive shift register
    reg [2:0] bit_cnt;       // bits into the current byte, 0..7
    reg [7:0] len_cnt;       // payload bytes still to come
    reg [15:0] age;          // cycles since the frame started: the watchdog
    reg [7:0] data_r;        // the last completed payload byte
    reg       valid_r;
    reg       ok_r;
    reg       err_r;
    reg [3:0] state;         // one-hot: HUNT, LEN, DATA, TAIL

    localparam integer HUNT = 0;
    localparam integer LEN  = 1;
    localparam integer DATA = 2;
    localparam integer TAIL = 3;

    // ---- the byte the shift register is about to hold ----------------------
    // Everything that captures a byte captures *this*, so that a capture and
    // the shift that completes it happen in the same cycle.
    wire [7:0] sh_nxt = {sh[6:0], rx};

    // ---- control terms, each held out as its own cell ----------------------
    (* keep *) wire byte_done  = (bit_cnt == 3'd7);
    (* keep *) wire sync_here  = (sh_nxt == DELIMITER);
    (* keep *) wire len_zero   = (sh_nxt == 8'd0);
    (* keep *) wire len_last   = (len_cnt == 8'd1);
    (* keep *) wire expired    = (age == 16'hFFFF);

    (* keep *) wire take_len   = state[LEN]  & byte_done;
    (* keep *) wire take_byte  = state[DATA] & byte_done;
    (* keep *) wire close_tail = state[TAIL] & byte_done;

    (* keep *) wire leave_byte = byte_done | expired;
    (* keep *) wire leave_data = (take_byte & len_last) | expired;
    (* keep *) wire to_tail    = take_byte & len_last & ~expired;
    (* keep *) wire to_hunt    = close_tail | (take_len & len_zero) | expired;

    // ---- the frame machine -------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sh      <= 8'h00;
            bit_cnt <= 3'd0;
            len_cnt <= 8'd0;
            age     <= 16'd0;
            data_r  <= 8'h00;
            valid_r <= 1'b0;
            ok_r    <= 1'b0;
            err_r   <= 1'b0;
            state   <= 4'b0001;
        end else begin
            // The shift register never stops: hunting for the delimiter is
            // done one bit at a time, so there is no byte boundary to respect
            // until one has been found.
            sh <= sh_nxt;

            // The bit counter runs inside a byte and is parked at zero while
            // hunting, so that the first bit after the delimiter is bit 0 of
            // the length field.
            if (state[HUNT]) begin
                bit_cnt <= 3'd0;
            end else begin
                bit_cnt <= bit_cnt + 3'd1;
            end

            // The watchdog: cleared while hunting, one increment per cycle in
            // every other state.  This is the design's only carry chain, and it
            // is *only* a counter -- nothing reads its value except the
            // comparison above.
            if (state[HUNT] | expired) begin
                age <= 16'd0;
            end else begin
                age <= age + 16'd1;
            end

            // The payload counter: loaded from the length field, then one
            // decrement per completed payload byte.
            if (take_len) begin
                len_cnt <= sh_nxt;
            end else if (take_byte) begin
                len_cnt <= len_cnt - 8'd1;
            end

            // The output byte register and its strobe.
            if (take_byte) begin
                data_r <= sh_nxt;
            end
            valid_r <= take_byte;

            // The two frame verdicts, one cycle each.
            ok_r  <= close_tail &  sync_here & ~expired;
            err_r <= (close_tail & ~sync_here)
                   | (take_len   &  len_zero)
                   | expired;

            // The one-hot state vector.  Every next-state term is a function
            // of at most five signals, which is what the `keep` attributes on
            // the control terms above are for.
            state[HUNT] <= (state[HUNT] & ~sync_here) | to_hunt;
            state[LEN]  <= (state[LEN]  & ~leave_byte)
                         | (state[HUNT] &  sync_here);
            state[DATA] <= (state[DATA] & ~leave_data)
                         | (take_len    & ~len_zero);
            state[TAIL] <= (state[TAIL] & ~leave_byte) | to_tail;
        end
    end

    assign rx_data    = data_r;
    assign rx_valid   = valid_r;
    assign frame_ok   = ok_r;
    assign frame_err  = err_r;
    assign link_state = state;

endmodule

`default_nettype wire
