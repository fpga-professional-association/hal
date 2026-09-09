// ---------------------------------------------------------------------------
// traffic_fsm_recovered -- the design as reconstructed from the netlist ALONE.
//
// Nothing in this file was copied from design.v.  Every line traces back to
// something the tools showed, and guide.html says which step produced it:
//
//   ports        step 2  -- clk is the net on all eight clk pins, arst_n the net
//                           on all eight clrn pins, freeze the one input left
//   r,y,g,w      step 3  -- the 4-flip-flop strongly connected component
//   cnt[3:0]     step 3  -- the 4-flip-flop self-dependent chain, LSB first by
//                           dependency depth
//   encoding     step 4  -- the reachable states of the recovered relation are
//                           0000, 1100, 1010, 1001 in bit order (r,y,g,w)
//   next-state   step 4  -- the recovered transition relation
//   limit values step 5  -- the product machine: how many cycles each phase
//                           occupies before the state bits move
//   legal        step 4  -- every next-state LUT is gated by one common term
//                           that is true exactly on those four states
//   o0,o1,o2     step 6  -- the measured output pattern
//
// Names are invented here.  The netlist has none: the flip-flops are g1, g2,
// g4, g6 and g7, g9, g10, g11, and the ports are i0..i2 / o0..o2.
// ---------------------------------------------------------------------------

module traffic_fsm_recovered (
    input  wire clk,      // i1 -- drives every tennm_ff.clk
    input  wire arst_n,   // i2 -- drives every tennm_ff.clrn (active low, async)
    input  wire freeze,   // i0 -- inverted once, then used as the counter enable
    output wire o0,       // "red"    -- see the naming argument in guide.html
    output wire o1,       // "yellow"
    output wire o2        // "green"
);

    // -----------------------------------------------------------------------
    // The two register groups the dependency graph splits into.
    // -----------------------------------------------------------------------
    reg r, y, g, w;       // the SCC: netlist gates g1, g2, g6, g4
    reg [3:0] cnt;        // the chain: netlist gates g9, g10, g11, g7 (LSB..MSB)

    // The four reachable encodings, exactly as the recovered relation lists
    // them.  Note that this is one-hot with the first bit *inverted*, which is
    // how the machine can sit in a legal state after an all-zero reset.
    // Written in the netlist's own bit order, bit 0 first:
    // (g1, g2, g4, g6) = (r, y, w, g)
    //   phase A   0 0 0 0     decimal  0
    //   phase B   1 1 0 0              3
    //   phase C   1 0 0 1              9
    //   phase D   1 0 1 0              5
    wire phase_a = ~r;
    wire phase_b =  r &  y;
    wire phase_c =  r &  g;
    wire phase_d =  r &  w;

    // -----------------------------------------------------------------------
    // The common gating term.  In the netlist this is one LUT (g13, driving
    // net n11) that every one of the four next-state LUTs reads, and it is
    // true exactly on the four encodings above -- a legality check.  A state
    // that is not one of the four is driven back to 0000 on the next edge.
    // -----------------------------------------------------------------------
    wire legal = (~r & ~y & ~g & ~w)
               | ( r & ~y & (w ^ g))
               | ( r &  y & ~w & ~g);

    // -----------------------------------------------------------------------
    // Dwell limit per phase.  The netlist splits the comparison in two: one
    // LUT (g12 -> n7) compares cnt[2:0] against a phase-dependent constant and
    // one LUT (g8 -> n2) compares cnt[3] against another.  Written out, the
    // constant is:
    // -----------------------------------------------------------------------
    wire [3:0] limit = phase_a ? 4'd9
                     : phase_b ? 4'd2
                     : phase_c ? 4'd12
                     :           4'd4;

    wire expired = (cnt == limit);
    wire advance = expired & ~freeze;

    // -----------------------------------------------------------------------
    // Sequential part.  Asynchronous, active-low clear on every flip-flop; the
    // counter additionally has a clock enable, the state register does not.
    // -----------------------------------------------------------------------
    always @(posedge clk or negedge arst_n) begin
        if (!arst_n) begin
            r <= 1'b0; y <= 1'b0; g <= 1'b0; w <= 1'b0;
            cnt <= 4'd0;
        end else begin
            if (!legal) begin
                r <= 1'b0; y <= 1'b0; g <= 1'b0; w <= 1'b0;
            end else if (advance) begin
                // The rotation, read straight off the four next-state LUTs.
                r <= ~w;      // leave "not A" unless we are in D
                y <= ~r;      // B follows A
                g <=  y;      // C follows B
                w <=  g;      // D follows C
            end
            if (!freeze) begin
                cnt <= expired ? 4'd0 : cnt + 4'd1;
            end
        end
    end

    // -----------------------------------------------------------------------
    // Outputs -- pure functions of the state register (a Moore machine).
    // -----------------------------------------------------------------------
    assign o0 = ~r | y;   // high in phases A and B
    assign o1 =  y | w;   // high in phases B and D
    assign o2 =  g;       // high in phase C

endmodule
