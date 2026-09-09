// ---------------------------------------------------------------------------
// recovered.v -- the design, rewritten from the NETLIST ALONE.
//
// Every port, register group, width and constant below was derived from
// `netlist.hal.v` by the steps in guide.html.  The evidence for each claim is
// cited inline as `artifacts/<file>`; nothing here was read out of `design.v`,
// `spec.md` or `reference.py`.
//
//   * ports and clocking -- artifacts/02_clock_reset.txt: one clock net on all
//                           eight clk pins, one active-low asynchronous clear
//                           on all eight clrn pins, every other control pin
//                           tied to a constant.
//   * register grouping  -- artifacts/03_gate_sccs.txt and
//                           artifacts/05_register_sccs.txt: one 4-flop feedback
//                           group, one self-holding bit, three pure delay flops
//                           in two chains.
//   * the counter        -- artifacts/07_word_semantics.txt part A: the whole
//                           32-entry transition relation of the 4-flop group,
//                           enumerated, not sampled.
//   * the hysteresis bit -- artifacts/07_word_semantics.txt part B: all 32
//                           (flag, counter) combinations.
//   * the edge detector  -- artifacts/07_word_semantics.txt part C.
//   * the timing numbers -- artifacts/08_probe.txt: 18-cycle latency each way,
//                           15-cycle minimum accepted press.
//
// Three things in here were *discovered*, not assumed, and they are the reason
// this file does not look like a textbook debouncer:
//
//   1. There is NO comparator cell for "counter == 15" and none for
//      "counter == 0".  Quartus folded both comparisons into the single
//      5-input cell that computes the hysteresis bit's next state
//      (`i38~0`), and folded the same two comparisons again into each of the
//      four counter cells.  The RTL below writes them as named wires because
//      that is readable; the netlist has no such nets.
//   2. There is NO carry chain.  A saturating up/down counter is not an
//      adder as far as this synthesis run is concerned: each bit is one
//      5-input LUT of (all four counter bits, direction).
//      `hal_agilex recognize` reports zero carry chains, correctly.
//   3. Nothing distinguishes the two-flop synchronizer from the two-flop
//      edge-detector pair structurally.  The edge detector was identified
//      because a cell in the netlist COMPARES its two stages; the
//      synchronizer was identified because its chain is fed by a primary
//      input and nothing compares its stages.  That second identification is
//      an argument from intent, and it is the weakest claim in this file.
//
// Behaviour is checked against the export in check.py.
// ---------------------------------------------------------------------------

module recovered_shift_debouncer (
    input  wire clk,        // the only net on all 8 tennm_ff clk pins
    input  wire rst_n,      // the only net on all 8 tennm_ff clrn pins (active low)
    input  wire btn_raw,    // the only remaining input; it feeds exactly one flop
    output wire btn_state,  // driven straight from a flip-flop q pin
    output wire btn_rise    // driven by the one ALM whose output leaves the module
);

    // -- the registers, as grouped by the dependency graph -------------------

    // Group A: two flops, no feedback, no enable, chained head-to-tail, with
    //          the head's D pin connected directly to the primary input.
    //          artifacts/04_register_graph.txt rows sync[0], sync[1].
    reg [1:0] sync;

    // Group B: four flops that all appear in each other's next-state cone AND
    //          in their own.  artifacts/03_gate_sccs.txt SCC 0 (8 gates,
    //          4 of them flops).  Bit order recovered from the transition
    //          path, not from names: artifacts/07_word_semantics.txt part A.
    reg [3:0] cnt;

    // Group C: one flop whose D depends on itself and on all four of group B.
    //          artifacts/05_register_sccs.txt, "self-holding singleton".
    reg       state;

    // Group D: one more pure delay flop, fed by group C.
    reg       state_d;

    // -- what the netlist compares --------------------------------------------
    // Not present as nets.  Both comparisons were absorbed into the LUT masks;
    // they are written out here because the enumerated truth tables in
    // artifacts/07_word_semantics.txt say the cells behave exactly like this.
    wire at_max = (cnt == 4'hF);   // the fixed point of the up direction
    wire at_min = (cnt == 4'h0);   // the fixed point of the down direction

    // -- outputs ---------------------------------------------------------------
    assign btn_state = state;                 // q pin straight to the port
    assign btn_rise  = state & ~state_d;       // lut_mask 64'h2222...2222 over
                                               // (dataa=state, datab=state_d)

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // Every flop has clrn = rst_n, no set pin and no asynchronous load,
            // so the whole state clears to zero.  Nothing in the netlist can
            // reset to anything else.
            sync    <= 2'b00;
            cnt     <= 4'h0;
            state   <= 1'b0;
            state_d <= 1'b0;
        end
        else begin
            // Group A: a two-deep delay line off the raw pin.
            sync <= {sync[0], btn_raw};

            // Group B, from the enumerated transition relation:
            //   with sync[1] = 1 the 16 states form the single path
            //   0000 -> 0001 -> ... -> 1111 -> (holds);
            //   with sync[1] = 0 the same 16 states form the reverse path
            //   1111 -> 1110 -> ... -> 0000 -> (holds).
            // A path that ends in a hold is a clamp, not a wrap.
            if (sync[1] && !at_max)
                cnt <= cnt + 4'd1;
            else if (!sync[1] && !at_min)
                cnt <= cnt - 4'd1;

            // Group C, from the 32-row table: SET at counter value 15, CLEAR
            // at counter value 0, HOLD at all fourteen values in between.
            // The two values that move it are exactly the counter's two end
            // stops -- hysteresis.
            if (at_max)
                state <= 1'b1;
            else if (at_min)
                state <= 1'b0;

            // Group D.
            state_d <= state;
        end
    end

endmodule
