// ---------------------------------------------------------------------------
// recovered.v -- the design, rewritten from the NETLIST ALONE.
//
// Every name, width and constant in this file was derived from
// `netlist_anon.hal.v` (the name-stripped export) by the steps in guide.html:
//
//   * ports          -- from probe.py: which port drives every flip-flop clock
//                       pin, which drives every clear pin, which one makes the
//                       outputs move, and where each remaining input lands in
//                       the frame.
//   * register groups-- from analyze.py: the SCCs of the gate graph and the
//                       register-to-register dependency graph.
//   * next-state
//     equations      -- from the Boolean functions hal_agilex attached to each
//                       tennm_lcell_comb's lut_mask (artifacts/07_lut_functions.txt).
//   * frame layout   -- from probe.py's slot sampling.
//
// It is written to match the netlist's STRUCTURE, not the original author's
// style.  Two places where that matters, both discovered rather than assumed:
//
//   1. The shift register is NINE bits, not ten.  The tenth bit of the original
//      was a constant and synthesis deleted it; what remains is a nine-deep
//      chain fed by a constant.
//   2. The shift register stores the COMPLEMENT of the line.  The output cell
//      (`g_014` in the anonymised netlist, lut_mask 64'h5555...) is a pure
//      inverter, the load path takes ~data, and the fill bit shifted in at the
//      top is 0.  Nothing in the netlist stores the frame the right way up.
//
// Behaviour is checked against the export in check.py.
// ---------------------------------------------------------------------------

module recovered_top (
    input  wire       clk,       // PORT_02: the only net on all 18 clk pins
    input  wire       rst_n,     // PORT_11: the only net on all 18 clrn pins
    input  wire       start,     // PORT_09: the only remaining input that makes
                                 //          an output move
    input  wire [7:0] data,      // PORT_06,04,03,08,01,12,00,10 -- in that
                                 //          order on the wire, so this is the
                                 //          bus, least significant bit first
    output wire       serial,    // PORT_07: carries the frame
    output wire       status     // PORT_05: high for 161 cycles per frame
);

    // -- registers, as grouped by the register-dependency graph ---------------

    // Group A: 4 flip-flops, each with self-feedback, D = f(busy, group A).
    //          Increments every cycle while busy; resets to 0 when idle.
    reg [3:0] prescale;

    // Group B: 4 flip-flops, each with self-feedback, D = f(busy, group B),
    //          but ENABLE = f(busy, group A).  Gated by group A, therefore the
    //          slower of the two.
    reg [3:0] slot;

    // Group C: 9 flip-flops with NO self-feedback -- each D depends on exactly
    //          one other flip-flop of the group.  A chain, i.e. a serializer.
    //          Holds the complement of the line, see the header.
    reg [8:0] sr;

    // Group D: one flip-flop whose D depends on itself and on group B.
    reg       busy;

    // -- the two enable nets the netlist actually has -------------------------

    wire tick  = &prescale;                 // g_005: AND of all four group-A bits
    wire load  = ~busy & start;             // the (~busy & start) half of the
    wire shift =  busy & tick;              // shift-register enable cell
    // (the netlist has one cell computing `load | shift` and one computing
    //  `~busy | tick`; both are shown in artifacts/07_lut_functions.txt)

    assign serial = ~sr[0];                 // the netlist's output cell is an inverter
    assign status = busy;

    // -- next state, read off the LUT masks ----------------------------------

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // Every flip-flop has clrn = rst_n and no set, so reset is 0
            // everywhere.  In the complemented shift register, all-zero means
            // the line sits high.
            prescale <= 4'd0;
            slot     <= 4'd0;
            sr       <= 9'd0;
            busy     <= 1'b0;
        end
        else begin
            // busy: D = busy ? ~(slot == 10) : start.
            // The constant 10 comes from the 6-input mask of the cell that
            // drives this flop -- it is a comparator against 4'b1010.
            busy     <= busy ? ~(slot == 4'd10) : start;

            // prescale: D[i] = busy & (prescale[i] ^ carry_in), i.e. a plain
            // 4-bit increment ANDed with busy.  No terminal-count comparison
            // anywhere in its next state: it wraps because it is 4 bits wide.
            prescale <= busy ? prescale + 4'd1 : 4'd0;

            // slot: same increment structure, but the three upper flops are
            // enabled by (~busy | tick), so it only advances on a tick.
            if (!busy)      slot <= 4'd0;
            else if (tick)  slot <= slot + 4'd1;

            // the chain
            if (load)       sr <= {~data, 1'b1};   // ~data[7:0], then the start
                                                   // bit -- inverted, so 1
            else if (shift) sr <= {1'b0, sr[8:1]}; // fill 0 = line high
        end
    end

endmodule
